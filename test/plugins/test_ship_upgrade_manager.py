import json
import os
import sys
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from lib.PluginBase import PluginManifest
from lib.PluginManager import PluginManager
from lib.Event import GameEvent
from plugins.ship_upgrade_manager.ship_upgrade_manager import (
    BrokerQueryParams,
    BuildAdviceParams,
    MissingModulesParams,
    RemainingUpgradesParams,
    PrepareJumpParams,
    SessionActionParams,
    ShipUpgradeManagerPlugin,
    StateQueryParams,
)
from plugins.ship_upgrade_manager.parsers import PlanParseError, parse_plan_input


@pytest.fixture(autouse=True)
def _empty_journal_dir(tmp_path, monkeypatch):
    """Keep journal reads hermetic; journal-specific tests override the path."""
    import plugins.ship_upgrade_manager.ship_upgrade_manager as sum_module

    empty = tmp_path / "no-journals"
    empty.mkdir()
    monkeypatch.setattr(sum_module, "get_ed_journals_path", lambda _config: str(empty))
    monkeypatch.setattr(sum_module.PluginHelper, 'PLUGIN_DATA_PATH', str(tmp_path / 'plugin-data'))
    monkeypatch.setattr("lib.PluginManager.save_config", lambda _config: None)


class _PluginManager:
    def update_plugin_setting(self, *_args):
        return True

    def republish_settings(self) -> None:
        pass

    def publish_plugin_ui(self, _plugin):
        pass

    def publish_plugin_settings(self, _plugin):
        pass


class _Helper:
    _plugin_manager = _PluginManager()

    def __init__(self, data_path: Path):
        self.data_path = data_path
        self._event_manager = None
        self.events = []

    def get_plugin_data_path(self):
        return str(self.data_path)

    def get_current_state(self):
        return self._event_manager.get_current_state() if self._event_manager is not None else ([], {})

    def register_projection(self, _projection):
        pass

    def register_action(self, **_kwargs):
        pass

    def register_sideeffect(self, _sideeffect):
        pass

    def register_event(self, *_args):
        pass

    def dispatch_event(self, event):
        self.events.append(event)

    def register_status_generator(self, _generator):
        pass


def _plugin(data_path: Path, *, running: bool = True) -> ShipUpgradeManagerPlugin:
    plugin = ShipUpgradeManagerPlugin(
        PluginManifest(
            '{"guid":"test-guid","name":"Test","version":"1.0.0"}'
        )
    )
    plugin.on_chat_start(_Helper(data_path))
    if not running:
        plugin.on_chat_stop(plugin.helper)
    manager = PluginManager({"plugin_settings": {}})
    manager.plugin_list[plugin.plugin_manifest.guid] = plugin
    manager.register_settings()
    return plugin


def test_support_link_is_translated_and_settings_only(tmp_path):
    plugin = _plugin(tmp_path, running=False)
    assert plugin.get_ui_settings()['header_link'] == {
        'text': '@support.prompt', 'label': '@support.link', 'url': 'https://ko-fi.com/thyast',
    }
    translations = plugin.get_ui_translations()
    assert translations['en']['support']['prompt'] == 'Enjoy this plugin?'
    assert translations['fr']['support']['link'] == 'Soutenez son développement'
    assert all('header_link' not in view for view in plugin.get_ui_views())


def test_route_uses_the_same_settings_grid_before_during_and_after_runtime(tmp_path, monkeypatch):
    plugin = _plugin(tmp_path, running=False)
    fields = [plugin._paragraph('route_action_0', 'plugin.sum.route.engineer_visit', params={'target': 'Selene'})]
    monkeypatch.setattr(plugin, '_route_fields', lambda: fields)

    def exported_route():
        settings = plugin.get_ui_settings()
        tab = next(tab for tab in settings['tabs'] if tab['key'] == 'route')
        section = next(section for section in plugin.get_ui_views()[0]['sections'] if section['key'] == 'route')
        assert section['grids'] == tab['grids'] == ['route']
        assert 'content' not in section
        grid = next(grid for grid in settings['grids'] if grid['key'] == 'route')
        assert grid['fields'][0]['children'][0]['type'] == 'heading'
        assert 'content' not in grid
        return grid

    plugin.settings_config = plugin._build_settings_config()
    offline = exported_route()
    plugin.on_chat_start(_Helper(tmp_path))
    assert exported_route() == offline
    plugin.on_chat_stop(plugin.helper)
    assert exported_route() == offline


def test_current_loadout_action_reports_technical_item_id_and_engineering(tmp_path: Path):
    class FakeEventManager:
        def get_current_state(self):
            return [], {
                "Loadout": {
                    "Ship": "smallcombat01_nx",
                    "Modules": [
                        {
                            "Slot": "Armour",
                            "Item": "smallcombat01_nx_armour_reactive",
                            "Engineering": {
                                "BlueprintName": "Armour_Thermic",
                                "Level": 5,
                            },
                        }
                    ],
                }
            }

    plugin = ShipUpgradeManagerPlugin(
        PluginManifest(
            '{"guid":"test-guid","name":"Test","version":"1.0.0"}'
        )
    )
    helper = _Helper(tmp_path)
    helper._event_manager = FakeEventManager()
    plugin.helper = helper

    class Args:
        slot = "Armour"

    result = plugin._current_loadout_action(Args(), {})

    assert "Kestrel Mk II" in result
    assert "Reactive Surface Composite" in result
    assert "smallcombat01_nx_armour_reactive" in result
    assert "Thermic" in result
    assert "grade 5" in result


@pytest.mark.parametrize("action", ["import_plan", "new_import", "save_plan"])
def test_runtime_rejects_import_and_restores_config_controls(tmp_path: Path, action: str):
    plugin = _plugin(tmp_path, running=False)
    plugin.on_settings_button("import_plan")
    plugin.settings["plan_input"] = json.dumps({"ship_model": "Python", "plan_name": "Runtime", "steps": [{"id": "fsd"}]})
    plugin.on_chat_start(_Helper(tmp_path))
    assert "import" not in {grid["key"] for grid in plugin.settings_config["grids"]}
    assert "header_action" not in plugin._grid("plans")
    with pytest.raises(ValueError, match="plugin.sum.runtime.importUnavailable"):
        plugin.on_settings_button(action)
    assert plugin.list_plans() == []
    assert plugin._field("plans", "plans_status")["content"] == "plugin.sum.runtime.importUnavailable"
    plugin.on_chat_stop(plugin.helper)
    assert "plan_input" not in {field["key"] for field in plugin._grid("plans")["fields"]}
    plugin.on_settings_button("import_plan")
    assert plugin._field("plans", "plan_input")["type"] == "textarea"
    plugin.on_settings_button("save_plan")
    assert plugin.list_plans()[0]["plan_name"] == "Runtime"


def test_runtime_screen_follows_ship_and_commander(tmp_path: Path):
    plugin = _plugin(tmp_path)
    python_id = plugin.import_plan("Python", "Python plan", {"steps": [{"id": "fsd", "item": "int_hyperdrive_size5_class5"}]})
    krait_id = plugin.import_plan("krait_mkii", "Krait plan", {"steps": [{"id": "shield", "item": "int_shieldgenerator_size5_class5"}]})
    plugin.start_session("Python plan", "7", "Python One")
    plugin.start_session("Python plan", "9", "Python Two")
    plugin.start_session("Krait plan", "8", "Krait One")
    plugin.settings["plan_filter"] = "does not match"
    assert plugin._field("session", "session_summary")["content"] == "plugin.sum.errNoShip"
    assert plugin._field("plans", "available_plans")["items"] == []
    plugin._handle_ship_event({"event": "LoadGame", "FID": "F1", "ShipID": 7, "Ship": "python"})
    plugin._broker_set_status("guardian_fsd_booster", "unlocked")
    plugin._publish_status()
    plans = plugin._field("plans", "available_plans")
    assert [row["key"] for row in plans["items"]] == [python_id]
    assert [entry["ship"] for entry in plans["items"][0]["progress"]] == ["Python One"]
    assert [action["action"] for action in plans["row_actions"]] == ["start_plan_session"]
    assert plugin._field("session", "session_summary")["params"]["plan"] == "Python plan"
    assert plugin._field("session", "session_modules")["items"][0]["key"] == "fsd"
    broker = plugin._field("broker", "broker_unlocks")
    assert next(row for row in broker["items"] if row["key"] == "guardian_fsd_booster")["meta"] == "plugin.sum.broker.unlocked"
    plugin._handle_ship_event({"event": "LoadGame", "FID": "F2", "ShipID": 8, "Ship": "krait_mkii"})
    assert [row["key"] for row in plans["items"]] == [krait_id]
    assert plugin._field("session", "session_summary")["params"]["plan"] == "Krait plan"
    assert next(row for row in broker["items"] if row["key"] == "guardian_fsd_booster")["meta"] == "plugin.sum.broker.unknown"
    plugin._handle_ship_event({"event": "TechnologyBroker", "ItemsUnlocked": [{"Name": "int_guardianfsdbooster_size5"}]})
    assert next(row for row in broker["items"] if row["key"] == "guardian_fsd_booster")["meta"] == "plugin.sum.broker.unlocked"
    plugin._handle_ship_event({"event": "ShipyardSwap", "ShipID": 10, "ShipType": "python"})
    assert plugin._field("session", "session_summary")["content"] == "plugin.sum.noSession"
    assert plugin._field("session", "session_modules")["items"] == []


def test_runtime_plan_activation_uses_selected_id_with_duplicate_names(tmp_path: Path):
    plugin = _plugin(tmp_path)
    selected = plugin.import_plan("Python", "Combat", {"steps": [{"id": "python-module"}]})
    plugin.import_plan("krait_mkii", "Combat", {"steps": [{"id": "krait-module"}]})
    plugin._handle_ship_event({"event": "LoadGame", "FID": "F1", "ShipID": 7, "Ship": "python"})
    plugin.on_settings_button(f"start_plan_session:{selected}")
    session = plugin.get_session()
    assert session["plan_id"] == selected
    assert session["ship_instance_id"] == "7"
    assert plugin._field("session", "session_modules")["items"][0]["key"] == "python-module"


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


def test_import_tunnel_imports_directly(tmp_path: Path):
    plugin = _plugin(tmp_path, running=False)
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

    plugin.on_settings_button("save_plan")

    assert plugin._import_state == "idle"
    assert plugin.list_plans()[0]["plan_name"] == "PvE"
    assert "plan_input" not in plugin.get_ui_state()
    assert plugin.settings["plan_input"] == ""
    assert plugin._ui_notification == ("plugin.sum.msg.planUpdated", {"plan": "PvE"})
    assert plugin._field("plans", "available_plans")["items"][0]["highlighted"] is True


def test_import_tunnel_imports_migrates_plan(tmp_path: Path):
    plugin = _plugin(tmp_path, running=False)
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
    plugin.on_settings_button("save_plan")

    assert plugin._import_state == "idle"
    assert plugin.list_plans()[0]["plan_version"] == 2
    session = plugin.get_session()
    assert "fsd" in session["completed_steps"]
    assert plugin._ui_notification == ("plugin.sum.msg.planUpdated", {"plan": "PvE"})
    assert "import_done" not in {field["key"] for field in plugin._grid("plans")["fields"]}


def test_import_tunnel_reports_invalid_input_and_stays_in_data_state(tmp_path: Path):
    plugin = _plugin(tmp_path, running=False)
    plugin.on_settings_button("import_plan")
    plugin.settings["plan_input"] = "not json"

    plugin.on_settings_button("save_plan")

    assert plugin._import_state == "data"
    error_field = plugin._field("plans", "import_error")
    assert error_field["content"] == "plugin.sum.errParse"
    assert "detail" in error_field["params"]
    assert plugin.get_ui_state()["plan_input"] == "not json"


def test_import_tunnel_cancel_returns_to_idle_and_clears_input(tmp_path: Path):
    plugin = _plugin(tmp_path, running=False)
    plugin.on_settings_button("import_plan")
    plugin.settings["plan_input"] = "junk"

    plugin.on_settings_button("cancel_import")

    assert plugin._import_state == "idle"
    assert plugin.settings["plan_input"] == ""
    grid_keys = [grid["key"] for grid in plugin.settings_config["grids"]]
    assert "import" not in grid_keys
    plans_grid = next(g for g in plugin.settings_config["grids"] if g["key"] == "plans")
    assert plans_grid["header_action"]["key"] == "import_plan"


def test_single_import_entry_point_opens_inline_form(tmp_path: Path):
    plugin = _plugin(tmp_path, running=False)

    grid_keys = [grid["key"] for grid in plugin.settings_config["grids"]]
    assert "import" not in grid_keys
    plans_grid = next(g for g in plugin.settings_config["grids"] if g["key"] == "plans")
    assert plans_grid["header_action"]["key"] == "import_plan"
    assert plans_grid["header_action"]["label"] == "plugin.sum.btn.addPlan"

    plugin.on_settings_button("import_plan")

    grid_keys = [grid["key"] for grid in plugin.settings_config["grids"]]
    assert "import" not in grid_keys
    plans_grid = next(g for g in plugin.settings_config["grids"] if g["key"] == "plans")
    assert plans_grid["header_action"]["key"] == "import_plan"
    assert [field["key"] for field in plans_grid["fields"]][:3] == ["plan_input", "save_plan", "cancel_import"]


def test_import_tunnel_before_chat_start_imports_from_config_state(tmp_path: Path):
    plugin = ShipUpgradeManagerPlugin(
        PluginManifest(
            '{"guid":"not-started-test-guid","name":"Ship Upgrade","version":"1.0.0"}'
        )
    )

    from lib.PluginManager import PluginManager
    manager = PluginManager({'plugin_settings': {}})
    manager.plugin_list[plugin.plugin_manifest.guid] = plugin
    manager.register_settings()

    plugin.on_settings_button("import_plan")
    _set_plan_input(
        plugin,
        {
            "ship_model": "Python",
            "plan_name": "Config plan",
            "steps": [{"id": "fsd", "item": "int_hyperdrive_size5_class5"}],
        },
    )
    plugin.on_settings_button("save_plan")

    assert plugin.list_plans()[0]["plan_name"] == "Config plan"
    assert plugin._import_state == "idle"


def test_plan_rows_expose_delete_action_and_delete_by_row(tmp_path: Path):
    plugin = _plugin(tmp_path, running=False)
    plan_id = plugin.import_plan("Python", "PvE", {"steps": [{"id": "fsd"}]})
    plugin._publish_status()

    plans_field = plugin._field("plans", "available_plans")
    assert [row["key"] for row in plans_field["items"]] == [plan_id]
    actions = {action["action"]: action for action in plans_field["row_actions"]}
    assert set(actions) == {"rename_plan", "start_plan_session", "delete_plan"}
    assert actions["rename_plan"]["icon"] == "edit"
    assert actions["rename_plan"]["inline_edit"] is True
    assert actions["rename_plan"]["secondary"] is True
    assert actions["start_plan_session"]["icon"] == "play_arrow"
    assert actions["delete_plan"]["danger"] is True
    assert actions["delete_plan"]["label"] == "plugin.sum.btn.delete"
    assert actions["delete_plan"]["secondary"] is True
    assert actions["delete_plan"]["confirmation"] == "plugin.sum.confirmDelete"
    assert plans_field["details_collapsible"] is True
    assert plans_field["items"][0]["params"]["sessions"] == 0

    plugin.on_settings_button(f"delete_plan:{plan_id}")

    assert plugin.list_plans() == []
    assert plugin._ui_notification == ("plugin.sum.msg.deleted", {"plan": "PvE"})
    assert "plans_status" not in {field["key"] for field in plugin._grid("plans")["fields"]}


def test_delete_unknown_plan_row_reports_error(tmp_path: Path):
    plugin = _plugin(tmp_path)

    plugin.on_settings_button("delete_plan:unknown-id")

    field = plugin._field("plans", "plans_status")
    assert field["content"] == "plugin.sum.errDelete"
    assert "detail" in field["params"]


@pytest.mark.parametrize("by_row", [True, False])
@pytest.mark.parametrize("archived", [True, False])
def test_deletion_cleans_sessions_and_progress_without_foreign_keys(tmp_path: Path, monkeypatch, by_row: bool, archived: bool):
    plugin = _plugin(tmp_path, running=False)
    removed_id = plugin.import_plan("Python", "Removed", {"steps": [{"id": "fsd"}]})
    retained_id = plugin.import_plan("Krait", "Retained", {"steps": [{"id": "shield"}]})
    plugin.start_session("Removed", "SHIP-1")
    plugin.complete_step("fsd")
    if archived:
        plugin.stop_session()
    plugin.start_session("Retained", "SHIP-2")
    original_get_db = plugin.get_db

    @contextmanager
    def legacy_database():
        with original_get_db() as db:
            db.execute("PRAGMA foreign_keys = OFF")
            yield db

    monkeypatch.setattr(plugin, "get_db", legacy_database)
    if by_row:
        plugin._delete_plan_by_id(removed_id)
    else:
        plugin.delete_plan("Removed")
    with plugin.get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM active_session WHERE plan_id = ?", (removed_id,)).fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM step_history").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM session_history WHERE plan_id = ?", (removed_id,)).fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM active_session WHERE plan_id = ?", (retained_id,)).fetchone()[0] == 1
    assert plugin.get_plan(retained_id)["plan_name"] == "Retained"


def test_delete_last_imported_plan_resets_import_state(tmp_path: Path):
    plugin = _plugin(tmp_path, running=False)
    plugin.on_settings_button("import_plan")
    _set_plan_input(
        plugin,
        {
            "ship_model": "Python",
            "plan_name": "PvE",
            "steps": [{"id": "fsd", "item": "int_hyperdrive_size5_class5"}],
        },
    )
    plugin.on_settings_button("save_plan")
    plan_id = plugin.list_plans()[0]["id"]

    plugin.on_settings_button(f"delete_plan:{plan_id}")

    assert plugin._import_state == "idle"
    assert plugin._last_import is None
    grid_keys = [grid["key"] for grid in plugin.settings_config["grids"]]
    assert "import" not in grid_keys


def test_last_import_persists_across_plugin_restart(tmp_path: Path):
    plugin = _plugin(tmp_path, running=False)
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
    plugin.on_settings_button("save_plan")

    restarted = ShipUpgradeManagerPlugin(
        PluginManifest('{"guid":"test-guid","name":"Test","version":"1.0.0"}')
    )
    restarted.on_chat_start(_Helper(tmp_path))
    restarted.on_chat_stop(restarted.helper)

    assert restarted._import_state == "idle"
    assert restarted._last_import["plan_name"] == "PvE"
    assert restarted._last_import["version"] == 2
    assert restarted._ui_notification is None
    assert {grid["key"] for grid in restarted.settings_config["grids"]} == {"plans", "session", "broker", "route"}
    assert not {"import_done", "new_import", "plan_input", "plans_status"} & {
        field["key"] for field in restarted._grid("plans")["fields"]
    }


def test_gameplay_prioritizes_route_then_progress_and_ignores_old_status(tmp_path: Path):
    plugin = _plugin(tmp_path, running=False)
    plugin.settings["plans_status"] = "plugin.sum.msg.imported"
    plugin._sync_grids()
    assert "plans_status" not in {field["key"] for field in plugin._grid("plans")["fields"]}
    for lifecycle in (lambda: None, lambda: plugin.on_chat_start(_Helper(tmp_path)),
                      lambda: plugin.on_chat_stop(plugin.helper)):
        lifecycle()
        sections = plugin.get_ui_views()[0]["sections"]
        assert [section["key"] for section in sections] == ["route", "session", "broker", "plans"]
        assert sections[1]["label"] == "@progress.title"
        assert "import" not in {grid["key"] for grid in plugin.settings_config["grids"]}
        assert plugin._ui_notification is None


def test_plan_filter_filters_rows_live_and_groups_by_ship(tmp_path: Path):
    plugin = _plugin(tmp_path, running=False)
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


def test_plan_filter_applies_through_config_updates(tmp_path: Path, monkeypatch):
    import lib.PluginManager as plugin_manager_module
    from lib.PluginManager import PluginManager

    guid = "f1d78e6b-3e3b-4dc6-a61c-bff3e2b2f11e"
    monkeypatch.setattr(plugin_manager_module, "save_config", lambda _config: None)
    emitted: list[str] = []
    monkeypatch.setattr(
        plugin_manager_module,
        "emit_message",
        lambda event, **_payload: emitted.append(event),
    )

    plugin = ShipUpgradeManagerPlugin(
        PluginManifest(json.dumps({"guid": guid, "name": "SUM", "version": "1.0.0"}))
    )
    plugin.on_chat_start(_Helper(tmp_path))
    plugin.on_chat_stop(plugin.helper)
    plugin.import_plan("Python", "Mining", {"steps": [{"id": "laser"}]})
    plugin.import_plan("Krait", "Combat", {"steps": [{"id": "cannon"}]})

    manager = PluginManager({"plugin_settings": {}})
    manager.plugin_list[guid] = plugin
    manager.register_settings()

    # The historical configuration channel remains supported.
    manager.on_settings_changed({"plugin_settings": {guid: {"plan_filter": "mining"}}})

    rows = plugin._field("plans", "available_plans")["items"]
    assert [row["title"] for row in rows] == ["Mining"]
    assert "plugin_settings_configs" in emitted


def test_plan_filter_uses_shared_ui_callback_without_persisting_search(tmp_path: Path, monkeypatch):
    plugin = _plugin(tmp_path, running=False)
    manager = plugin._manager
    assert manager is not None
    guid = plugin.plugin_manifest.guid
    plugin.import_plan("Python", "Mining", {"steps": [{"id": "laser"}]})
    plugin.import_plan("Krait", "Combat", {"steps": [{"id": "cannon"}]})
    monkeypatch.setattr("lib.PluginManager.save_config", lambda _config: pytest.fail("Search must not persist"))
    emitted = []
    monkeypatch.setattr("lib.PluginManager.emit_message",
                        lambda kind, **payload: emitted.append((kind, deepcopy(payload))))

    manager.on_ui_action(guid, "ui_changed", {"key": "plan_filter", "value": " MINING "}, "search")

    assert emitted[-1][1]["success"] is True
    assert [row["title"] for row in plugin._list_field("plans", "available_plans")["items"]] == ["Mining"]
    assert manager.config == {"plugin_settings": {}}
    assert "plan_filter" not in plugin.settings
    assert plugin.get_ui_state()["plan_filter"] == " MINING "
    assert [kind for kind, _ in emitted].count("plugin_ui") == 1
    settings = manager.plugin_settings_configs[guid]
    plans = next(grid for grid in settings["grids"] if grid["key"] == "plans")
    assert next(field for field in plans["fields"] if field["key"] == "available_plans")["items"][0]["title"] == "Mining"

    manager.on_settings_changed(manager.config)
    plugin._publish_status()
    assert plugin.get_ui_state()["plan_filter"] == " MINING "
    assert [row["title"] for row in plugin._list_field("plans", "available_plans")["items"]] == ["Mining"]
    manager.on_ui_action(guid, "ui_changed", {"key": "plan_filter", "value": 123}, "invalid")
    assert emitted[-1][1]["success"] is False
    assert plugin.get_ui_state()["plan_filter"] == " MINING "
    manager.on_ui_action(guid, "ui_changed", {"key": "plan_filter", "value": ""}, "clear")
    assert emitted[-1][1]["success"] is True
    assert len(plugin._list_field("plans", "available_plans")["items"]) == 2


def test_saved_legacy_filter_is_used_until_a_transient_ui_edit(tmp_path: Path):
    plugin = _plugin(tmp_path, running=False)
    manager = plugin._manager
    assert manager is not None
    guid = plugin.plugin_manifest.guid
    plugin.import_plan("Python", "Mining", {"steps": [{"id": "laser"}]})
    plugin.import_plan("Krait", "Combat", {"steps": [{"id": "cannon"}]})
    plugin.settings["plan_filter"] = "mining"
    plugin._publish_status()
    assert [row["title"] for row in plugin._list_field("plans", "available_plans")["items"]] == ["Mining"]
    manager.on_ui_action(guid, "ui_changed", {"key": "plan_filter", "value": ""}, "clear-legacy-filter")
    plugin._publish_status()
    assert len(plugin._list_field("plans", "available_plans")["items"]) == 2
    assert plugin.get_ui_state()["plan_filter"] == ""
    assert plugin.settings["plan_filter"] == "mining"


def test_import_input_uses_current_ui_state_and_clears_on_cancel_and_new_import(tmp_path: Path, monkeypatch):
    plugin = _plugin(tmp_path, running=False)
    manager = plugin._manager
    assert manager is not None
    guid = plugin.plugin_manifest.guid
    saved = []
    monkeypatch.setattr("lib.PluginManager.save_config", lambda config: saved.append(deepcopy(config)))
    emitted = []
    monkeypatch.setattr("lib.PluginManager.emit_message",
                        lambda kind, **payload: emitted.append((kind, deepcopy(payload))))

    def action(name, payload):
        manager.on_ui_action(guid, name, payload, name)
        assert emitted[-1][0] == "plugin_ui_result"
        assert emitted[-1][1]["success"] is True

    action("settings_button", {"key": "import_plan"})
    action("ui_changed", {"key": "plan_input", "value": "cancel this draft"})
    assert plugin.get_ui_state()["plan_input"] == "cancel this draft"
    assert manager.config["plugin_settings"][guid]["plan_input"] == "cancel this draft"
    action("settings_button", {"key": "cancel_import"})
    action("settings_button", {"key": "import_plan"})
    assert plugin.get_ui_state()["plan_input"] == ""

    payload = json.dumps({"ship_model": "Python", "plan_name": "UI plan",
                          "steps": [{"id": "fsd", "item": "int_hyperdrive_size5_class5"}]})
    action("ui_changed", {"key": "plan_filter", "value": "does-not-match"})
    action("ui_changed", {"key": "plan_input", "value": payload})
    action("settings_button", {"key": "save_plan"})
    assert plugin.list_plans()[0]["plan_name"] == "UI plan"
    assert plugin._import_state == "idle"
    assert plugin.get_ui_state()["plan_filter"] == ""
    assert "plan_input" not in plugin.get_ui_state()
    assert plugin.settings["plan_input"] == ""
    assert [row["title"] for row in plugin._field("plans", "available_plans")["items"]] == ["UI plan"]
    assert emitted[-1][1]["result"] == {
        "notification": f"plugin.{guid}.msg.planAdded", "params": {"plan": "UI plan"},
    }
    assert "notification" not in manager.plugin_ui[guid]
    assert plugin._ui_notification is None
    action("settings_button", {"key": "new_import"})
    assert emitted[-1][1]["result"] is None
    assert plugin.get_ui_state()["plan_input"] == ""
    assert manager.plugin_ui[guid]["state"]["plan_input"] == ""
    assert manager.config["plugin_settings"][guid]["plan_input"] == ""
    assert "plan_filter" not in manager.config["plugin_settings"][guid]
    assert len(saved) == 5


def test_common_publication_updates_settings_views_and_dynamic_progress_once(tmp_path: Path, monkeypatch):
    plugin = _plugin(tmp_path, running=False)
    manager = plugin._manager
    assert manager is not None
    guid = plugin.plugin_manifest.guid
    plugin.import_plan("Python", "Progress plan", {"steps": [
        {"id": "fsd", "item": "int_hyperdrive_size5_class5"},
        {"id": "shield", "item": "int_shieldgenerator_size5_class5"},
    ]})
    loadout = []
    monkeypatch.setattr(plugin, "_loadout_for_ship", lambda _ship: loadout)
    plugin.start_session("Progress plan", "7", "Test ship")
    emitted = []
    monkeypatch.setattr("lib.PluginManager.emit_message",
                        lambda kind, **payload: emitted.append((kind, deepcopy(payload))))
    monkeypatch.setattr("lib.PluginManager.save_config", lambda _config: pytest.fail("Publication must not persist"))

    loadout.append({"Slot": "FrameShiftDrive", "Item": "int_hyperdrive_size5_class5"})
    plugin.complete_step("fsd")
    emitted.clear()
    plugin._republish()

    assert [kind for kind, _ in emitted] == ["plugin_ui", "plugin_settings_configs", "plugin_model_providers"]
    settings = manager.plugin_settings_configs[guid]
    plans = next(grid for grid in settings["grids"] if grid["key"] == "plans")
    row = next(field for field in plans["fields"] if field["key"] == "available_plans")["items"][0]

    def nodes(content):
        for node in content:
            yield node
            yield from nodes(node.get("children", []))

    assert any(node["type"] == "progress" and node["value"] == 1 and node["maximum"] == 2
               for node in nodes(row["details"]))
    assert any(section.get("grids") == ["plans"] for section in manager.plugin_ui[guid]["views"][0]["sections"])


def test_runtime_route_is_display_only_without_implicit_calculation(tmp_path: Path, monkeypatch):
    plugin = _plugin(tmp_path)
    manager = plugin._manager
    assert manager is not None
    monkeypatch.setattr(plugin, "_resolve_current_ship_id", lambda: "7")
    monkeypatch.setattr(plugin, "_upgrade_target", lambda: {"ship_instance_id": "7"})
    calculations = []
    monkeypatch.setattr(plugin, "_build_advice_action", lambda: calculations.append(True))
    plugin._sync_grids()
    plugin.publish_ui()
    guid = plugin.plugin_manifest.guid
    route = next(grid for grid in manager.plugin_settings_configs[guid]["grids"] if grid["key"] == "route")
    assert all(field["type"] != "button" for field in route["fields"])
    assert any(field["type"] == "text" for field in route["fields"])
    assert calculations == []


def test_plan_rows_use_public_ship_names_and_match_display_name(tmp_path: Path):
    plugin = _plugin(tmp_path, running=False)
    plugin.import_plan("smallcombat01_nx", "Kestrel Build", {"steps": [{"id": "fsd"}]})
    plugin.import_plan("weird_unknown_ship", "Fallback Build", {"steps": [{"id": "fsd"}]})

    plugin._publish_status()

    rows = plugin._field("plans", "available_plans")["items"]
    groups = {row["title"]: row["group"] for row in rows}
    assert groups["Kestrel Build"] == "Kestrel Mk II"
    assert groups["Fallback Build"] == "Weird Unknown Ship"

    plugin.settings["plan_filter"] = "kestrel"
    plugin.on_settings_changed()
    rows = plugin._field("plans", "available_plans")["items"]
    assert [row["title"] for row in rows] == ["Kestrel Build"]


def test_plan_rows_expose_session_progress_and_next_module(tmp_path: Path):
    plugin = _plugin(tmp_path, running=False)
    plugin.import_plan(
        "Python",
        "PvE",
        {
            "steps": [
                {"id": "fsd", "item": "int_hyperdrive_size5_class5"},
                {
                    "id": "shield",
                    "item": "int_shieldgenerator_size5_class5",
                    "engineering": {
                        "BlueprintName": "ShieldGenerator_Reinforced",
                        "Level": 5,
                        "ExperimentalEffect": "special_shield_regenerative",
                    },
                },
            ]
        },
    )
    plugin.start_session("PvE", "SHIP-1", "Mina")
    plugin.complete_step("fsd")

    rows = plugin._field("plans", "available_plans")["items"]
    progress = rows[0]["progress"]
    assert progress[0]["ship"] == "Mina"
    # The bars reflect the ship's current loadout, which is empty here.
    assert progress[0]["modules_done"] == 0
    assert progress[0]["modules_total"] == 2
    assert progress[0]["modules_pct"] == 0
    assert progress[0]["eng_target"] == 5
    assert progress[0]["eng_current"] == 0
    assert progress[0]["eng_pct"] == 0
    assert progress[0]["paused"] is False
    assert progress[0]["next_label"] == "Shield Generator"
    assert progress[0]["next_key"] == "module.shieldgenerator"
    assert progress[0]["next_class"] == "5A"
    assert progress[0]["next_grade"] == 5
    assert progress[0]["next_engineering"] == "Reinforced"
    assert progress[0]["next_experimental_effect"] == "Fast Charge"


def test_next_module_label_falls_back_for_unknown_families(tmp_path: Path):
    plugin = _plugin(tmp_path, running=False)
    plugin.import_plan(
        "Python",
        "PvE",
        {"steps": [{"id": "mr", "item": "int_modulereinforcement_size2_class1"}]},
    )
    plugin.start_session("PvE", "SHIP-1")

    progress = plugin._field("plans", "available_plans")["items"][0]["progress"]
    assert progress[0]["next_label"] == "Module Reinforcement Package"
    assert progress[0]["next_key"] == "module.modulereinforcement"
    assert progress[0]["next_class"] == "2E"


def test_utility_modules_group_separately_from_hardpoints(tmp_path: Path):
    plugin = _plugin(tmp_path, running=False)
    plugin.import_plan(
        "Python",
        "PvE",
        {
            "steps": [
                {"id": "booster", "item": "hpt_shieldbooster_size0_class2"},
                {"id": "heatsink", "item": "hpt_heatsinklauncher_turret_tiny"},
                {"id": "laser", "item": "hpt_beamlaser_gimbal_large"},
            ]
        },
    )
    plugin.start_session("PvE", "SHIP-1")

    rows = plugin._field("session", "session_modules")["items"]
    by_title = {row.get("title_key", row["title"]): row for row in rows}
    assert by_title["module.shieldbooster"]["group"] == "plugin.sum.category.utilityMounts"
    assert by_title["module.heatsinklauncher"]["group"] == "plugin.sum.category.utilityMounts"
    assert by_title["module.beamlaser"]["group"] == "plugin.sum.category.hardpoints"

    plugin.set_session_paused(True)

    progress = plugin._field("plans", "available_plans")["items"][0]["progress"]
    assert progress[0]["paused"] is True


def test_rename_plan_via_row_action(tmp_path: Path):
    plugin = _plugin(tmp_path)
    plan_id = plugin.import_plan("Python", "PvE", {"steps": [{"id": "fsd"}]})

    plugin.on_settings_button(f"rename_plan:{plan_id}", "PvE Advanced")

    assert [plan["plan_name"] for plan in plugin.list_plans()] == ["PvE Advanced"]
    assert plugin.get_plan(plan_id)["id"] == plan_id
    assert plugin._ui_notification == ("plugin.sum.msg.renamed", {"name": "PvE Advanced"})

    plugin.on_settings_button(f"rename_plan:{plan_id}", "PvE")
    assert plugin.list_plans()[0]["plan_name"] == "PvE"


def test_rename_plan_to_existing_name_reports_error(tmp_path: Path):
    plugin = _plugin(tmp_path)
    first = plugin.import_plan("Python", "Mining", {"steps": [{"id": "laser"}]})
    plugin.import_plan("Python", "Combat", {"steps": [{"id": "cannon"}]})

    plugin.on_settings_button(f"rename_plan:{first}", "Combat")

    status = plugin._field("plans", "plans_status")
    assert status["content"] == "plugin.sum.errRename"
    assert "already exists" in status["params"]["detail"]
    assert {plan["plan_name"] for plan in plugin.list_plans()} == {"Mining", "Combat"}


def test_activate_plan_starts_session_for_current_ship(tmp_path: Path):
    plugin = _plugin(tmp_path)
    plan_id = plugin.import_plan("Python", "PvE", {"steps": [{"id": "fsd"}]})

    plugin.on_settings_button(f"start_plan_session:{plan_id}")

    status = plugin._field("plans", "plans_status")
    assert status["content"] == "plugin.sum.errNoShip"

    plugin._current_ship_id = "SHIP-9"
    plugin.on_settings_button(f"start_plan_session:{plan_id}")

    session = plugin.get_session()
    assert session is not None
    assert session["ship_instance_id"] == "SHIP-9"
    assert plugin._ui_notification == ("plugin.sum.msg.sessionStarted", {"plan": "PvE"})


def test_activate_plan_falls_back_to_journal_detection(tmp_path: Path, monkeypatch):
    import plugins.ship_upgrade_manager.ship_upgrade_manager as sum_module

    journal_dir = tmp_path / "journals"
    journal_dir.mkdir()
    newest = journal_dir / "Journal.2026-09-20T120000.01.log"
    newest.write_text(
        json.dumps({"event": "Fileheader"}) + "\n"
        + json.dumps({"event": "Music", "name": "MainTheme"}) + "\n",
        encoding="utf-8",
    )
    os.utime(newest, (2_000_000_000, 2_000_000_000))
    older = journal_dir / "Journal.2026-09-19T080000.01.log"
    older.write_text(
        json.dumps({"event": "Rank"}) + "\n"
        + json.dumps({"event": "Loadout", "Ship": "smallcombat01_nx", "ShipID": 42}) + "\n",
        encoding="utf-8",
    )
    os.utime(older, (1_900_000_000, 1_900_000_000))
    monkeypatch.setattr(sum_module, "get_ed_journals_path", lambda _config: str(journal_dir))

    plugin = _plugin(tmp_path)
    plan_id = plugin.import_plan("Python", "PvE", {"steps": [{"id": "fsd"}]})

    plugin.on_settings_button(f"start_plan_session:{plan_id}")

    session = plugin.get_session()
    assert session is not None
    assert session["ship_instance_id"] == "42"
    assert plugin._current_ship_id == "42"


def test_session_start_baselines_steps_satisfied_by_current_loadout(
    tmp_path: Path, monkeypatch
):
    import plugins.ship_upgrade_manager.ship_upgrade_manager as sum_module

    journal_dir = tmp_path / "journals"
    journal_dir.mkdir()
    modules = [
        {"Slot": "PowerPlant", "Item": "int_powerplant_size5_class5"},
        {
            "Slot": "FrameShiftDrive",
            "Item": "int_hyperdrive_size5_class5",
            "Engineering": {"BlueprintName": "FSD_LongRange", "Level": 3},
        },
        {
            "Slot": "ShieldGenerator",
            "Item": "int_shieldgenerator_size5_class5",
            "Engineering": {"BlueprintName": "ShieldGenerator_Reinforced", "Level": 5},
        },
    ]
    (journal_dir / "Journal.2026-09-20T120000.01.log").write_text(
        json.dumps(
            {"event": "Loadout", "Ship": "python", "ShipID": 7, "Modules": modules}
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sum_module, "get_ed_journals_path", lambda _config: str(journal_dir))

    plugin = _plugin(tmp_path)
    plugin.import_plan(
        "Python",
        "PvE",
        {
            "steps": [
                {"id": "pp", "item": "int_powerplant_size5_class5"},
                {
                    "id": "fsd",
                    "item": "int_hyperdrive_size5_class5",
                    "engineering": {"BlueprintName": "FSD_LongRange", "Level": 5},
                },
                {
                    "id": "shield",
                    "item": "int_shieldgenerator_size5_class5",
                    "engineering": {"BlueprintName": "ShieldGenerator_Reinforced", "Level": 5},
                },
            ]
        },
    )

    plugin.start_session("PvE", "7")

    session = plugin.get_session()
    assert set(session["completed_steps"]) == {"pp", "shield"}
    assert "fsd" not in session["completed_steps"]
    assert session["current_step"] == 1
    progress = plugin._field("plans", "available_plans")["items"][0]["progress"]
    assert progress[0]["modules_done"] == 3
    assert progress[0]["modules_total"] == 3
    assert progress[0]["modules_pct"] == 100
    # fsd carries the right blueprint at level 3 (< 5), shield at 5: 8/10.
    assert progress[0]["eng_current"] == 8
    assert progress[0]["eng_target"] == 10
    assert progress[0]["eng_pct"] == 80
    assert progress[0]["next_label"] == "Frame Shift Drive (FSD)"
    assert progress[0]["next_class"] == "5A"


def test_engineering_progress_requires_matching_blueprint(
    tmp_path: Path, monkeypatch
):
    import plugins.ship_upgrade_manager.ship_upgrade_manager as sum_module

    journal_dir = tmp_path / "journals"
    journal_dir.mkdir()
    modules = [
        {"Slot": "PowerPlant", "Item": "int_powerplant_size5_class5"},
        {
            "Slot": "ShieldGenerator",
            "Item": "int_shieldgenerator_size5_class5",
            "Engineering": {"BlueprintName": "ShieldGenerator_Blast", "Level": 5},
        },
    ]
    (journal_dir / "Journal.2026-09-20T120000.01.log").write_text(
        json.dumps(
            {"event": "Loadout", "Ship": "python", "ShipID": 7, "Modules": modules}
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sum_module, "get_ed_journals_path", lambda _config: str(journal_dir))

    plugin = _plugin(tmp_path)
    plugin.import_plan(
        "Python",
        "PvE",
        {
            "steps": [
                {"id": "pp", "item": "int_powerplant_size5_class5"},
                {
                    "id": "shield",
                    "item": "int_shieldgenerator_size5_class5",
                    "engineering": {"BlueprintName": "ShieldGenerator_Reinforced", "Level": 5},
                },
            ]
        },
    )

    plugin.start_session("PvE", "7")

    progress = plugin._field("plans", "available_plans")["items"][0]["progress"]
    assert progress[0]["modules_done"] == 2
    assert progress[0]["modules_pct"] == 100
    # Wrong blueprint: strict rule gives zero engineering points.
    assert progress[0]["eng_target"] == 5
    assert progress[0]["eng_current"] == 0
    assert progress[0]["eng_pct"] == 0


def test_engineering_catalogs_resolve_slef_ids_and_keep_unknown_ids_visible():
    import plugins.ship_upgrade_manager.ship_upgrade_manager as sum_module

    assert (
        sum_module._engineering_catalog_display_name(
            "Armour_Thermic", "blueprints.json", "fdname"
        )
        == "Thermal resistant"
    )
    assert (
        sum_module._engineering_catalog_display_name(
            "special_armour_chunky", "specials.json", "edname"
        )
        == "Deep Plating"
    )
    assert (
        sum_module._engineering_catalog_display_name(
            "unknown_engineering_id", "blueprints.json", "fdname"
        )
        == "unknown_engineering_id"
    )


def test_session_module_rows_diff_target_vs_current_loadout(
    tmp_path: Path, monkeypatch
):
    import plugins.ship_upgrade_manager.ship_upgrade_manager as sum_module

    journal_dir = tmp_path / "journals"
    journal_dir.mkdir()
    modules = [
        {"Slot": "PowerPlant", "Item": "int_powerplant_size5_class5"},
        {
            "Slot": "ShieldGenerator",
            "Item": "int_shieldgenerator_size5_class5",
            "Engineering": {
                "BlueprintName": "ShieldGenerator_Reinforced",
                "Level": 3,
                "ExperimentalEffect": "special_shield_regenerative",
            },
        },
    ]
    (journal_dir / "Journal.2026-09-20T120000.01.log").write_text(
        json.dumps(
            {"event": "Loadout", "Ship": "python", "ShipID": 7, "Modules": modules}
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sum_module, "get_ed_journals_path", lambda _config: str(journal_dir))

    plugin = _plugin(tmp_path)
    plugin.import_plan(
        "Python",
        "PvE",
        {
            "steps": [
                {"id": "pp", "item": "int_powerplant_size5_class5"},
                {
                    "id": "shield",
                    "item": "int_shieldgenerator_size5_class5",
                    "engineering": {
                        "BlueprintName": "ShieldGenerator_Reinforced",
                        "Level": 5,
                        "ExperimentalEffect": "special_shield_regenerative",
                    },
                },
                {
                    "id": "cannon",
                    "item": "int_cannon_fixed_medium_class2",
                },
            ]
        },
    )
    plugin.start_session("PvE", "7")

    rows = plugin._field("session", "session_modules")["items"]
    by_title = {row.get("title_key", row["title"]): row for row in rows}
    assert by_title["module.powerplant"]["meta"] == "plugin.sum.sessionStep.installed"
    assert by_title["module.powerplant"]["pending"] is False
    assert by_title["module.powerplant"]["status"] == "complete"
    assert by_title["module.powerplant"]["group"] == "plugin.sum.category.coreInternal"
    assert by_title["module.shieldgenerator"]["meta"] == "plugin.sum.sessionStep.targetProgress"
    assert by_title["module.shieldgenerator"]["params"] == {
        "blueprint": "Reinforced",
        "target": 5,
        "current": 3,
        "experimentalEffect": "Fast Charge",
    }
    assert by_title["module.shieldgenerator"]["pending"] is True
    assert by_title["module.shieldgenerator"]["status"] == "installed-pending"
    assert by_title["module.shieldgenerator"]["group"] == "plugin.sum.category.optionalInternal"
    assert by_title["module.cannon"]["meta"] == "plugin.sum.sessionStep.absent"
    assert by_title["module.cannon"]["pending"] is True
    assert by_title["module.cannon"]["status"] == "absent"
    assert by_title["module.cannon"]["group"] == "plugin.sum.category.optionalInternal"
    group_order: list[str] = []
    for row in rows:
        group = str(row["group"])
        if group not in group_order:
            group_order.append(group)
    assert group_order == [
        "plugin.sum.category.coreInternal",
        "plugin.sum.category.optionalInternal",
    ]


def test_session_module_rows_display_catalog_names_for_reactive_armour(
    tmp_path: Path, monkeypatch
):
    import plugins.ship_upgrade_manager.ship_upgrade_manager as sum_module

    journal_dir = tmp_path / "journals"
    journal_dir.mkdir()
    module = {
        "Slot": "Armour",
        "Item": "smallcombat01_nx_armour_reactive",
        "Engineering": {
            "BlueprintName": "Armour_Thermic",
            "Level": 5,
            "ExperimentalEffect": "special_armour_chunky",
        },
    }
    (journal_dir / "Journal.2026-09-20T120000.01.log").write_text(
        json.dumps(
            {"event": "Loadout", "Ship": "python", "ShipID": 7, "Modules": [module]}
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sum_module, "get_ed_journals_path", lambda _config: str(journal_dir))

    plugin = _plugin(tmp_path)
    plugin.import_plan(
        "Python",
        "Reactive armour",
        {
            "steps": [
                {
                    "id": "armour",
                    "slot": "Armour",
                    "item": "reactive_armour",
                    "engineering": {
                        "BlueprintName": "Armour_Thermic",
                        "Level": 5,
                        "ExperimentalEffect": "special_armour_chunky",
                    },
                }
            ]
        },
    )
    plugin.start_session("Reactive armour", "7")

    row = plugin._field("session", "session_modules")["items"][0]
    assert row["key"] == "armour"
    assert row["params"] == {
        "blueprint": "Thermal resistant",
        "target": 5,
        "experimentalEffect": "Deep Plating",
        "current": 5,
    }


def test_journal_watcher_dispatches_entries_in_config_state(tmp_path: Path, monkeypatch):
    import plugins.ship_upgrade_manager.ship_upgrade_manager as sum_module

    journal_dir = tmp_path / "journals"
    journal_dir.mkdir()
    monkeypatch.setattr(sum_module, "get_ed_journals_path", lambda _config: str(journal_dir))

    plugin = _plugin(tmp_path)
    plugin._current_ship_id = "7"
    # Back to config state (as after Run is stopped) so the watcher processes.
    plugin._watch_active = True
    watcher = sum_module._JournalWatcher(plugin)
    line = json.dumps(
        {
            "event": "Loadout",
            "Ship": "python",
            "ShipID": 7,
            "ShipName": "Mina",
            "Modules": [
                {"Slot": "PowerPlant", "Item": "int_powerplant_size5_class5"},
            ],
        }
    )

    watcher._dispatch(line)

    assert plugin._current_ship_name == "Mina"
    assert plugin._current_loadout_modules[0]["Item"] == "int_powerplant_size5_class5"

    # While the runtime runs, the watcher drops entries to avoid duplicates.
    plugin._watch_active = False
    watcher._dispatch(line.replace("Mina", "Other"))
    assert plugin._current_ship_name == "Mina"


def test_slef_kestrel_fixture_parses_full_engineering(tmp_path: Path):
    fixture = Path(__file__).parent / "fixtures" / "kestrel_mkii_slef.json"
    normalized = parse_plan_input(fixture.read_text(encoding="utf-8"))

    assert normalized["plan_name"] == "Imported Kestrel Mk II"
    assert normalized["ship_model"] == "smallcombat01_nx"
    assert len(normalized["steps"]) == 26

    engineering = {
        step["item"].lower(): step["engineering"]
        for step in normalized["steps"]
    }
    assert engineering["int_powerplant_size5_class5"]["BlueprintName"] == "PowerPlant_Armoured"
    assert engineering["int_powerplant_size5_class5"]["Level"] == 5
    assert engineering["int_powerplant_size5_class5"]["ExperimentalEffect"] == "special_powerplant_cooled"
    assert engineering["int_hyperdrive_overcharge_size4_class5"]["Level"] == 5
    # Cargo hatch and the guardian module carry no engineering.
    assert engineering["modularcargobaydoor"] is None
    assert engineering["int_guardianmodulereinforcement_size2_class1"] is None


def test_reactive_armour_target_matches_kestrel_loadout_name(tmp_path: Path):
    plugin = _plugin(tmp_path)
    step = {
        "id": "armour",
        "slot": "Armour",
        "item": "reactive_armour",
        "engineering": {"BlueprintName": "Armour_Thermic", "Level": 5},
    }
    loadout = [{
        "Slot": "Armour",
        "Item": "smallcombat01_nx_armour_reactive",
        "Engineering": {"BlueprintName": "Armour_Thermic", "Level": 5},
    }]

    assert plugin._module_display("smallcombat01_nx_armour_reactive")[0] == "Reactive Surface Composite"
    assert plugin._module_display("reactive_armour")[0] == "Reactive Surface Composite"
    assert plugin._module_display("smallcombat01_nx_armour_reactive")[1] == "module.smallcombat01nxarmourreactive"
    assert plugin._match_steps([step], loadout)[0]["installed"] is True
    assert plugin._match_steps([step], loadout)[0]["satisfied"] is True
    assert plugin._steps_satisfied_by_loadout([step], loadout) == ["armour"]


def test_next_step_action_uses_catalog_blueprint_and_experimental_effect(tmp_path: Path):
    plugin = _plugin(tmp_path)
    plugin.import_plan(
        "Python",
        "PvE",
        {
            "steps": [
                {"id": "fsd", "item": "int_hyperdrive_size4_class5"},
                {
                    "id": "armour",
                    "item": "reactive_armour",
                    "engineering": {
                        "BlueprintName": "Armour_Thermic",
                        "Level": 5,
                        "ExperimentalEffect": "special_armour_chunky",
                    },
                },
            ]
        },
    )
    plugin.start_session("PvE", "SHIP-1")
    plugin.complete_step("fsd")

    result = plugin._next_step_action()

    assert "Reactive Surface Composite" in result
    assert "Thermal resistant grade 5" in result
    assert "experimental effect Deep Plating" in result
    assert "Thermic" not in result
    assert "2 of 2" in result


def test_next_step_action_plain_module_has_no_engineering_clause(tmp_path: Path):
    plugin = _plugin(tmp_path)
    plugin.import_plan(
        "Python",
        "PvE",
        {"steps": [{"id": "pp", "item": "int_powerplant_size5_class5"}]},
    )
    plugin.start_session("PvE", "SHIP-1")

    result = plugin._next_step_action()

    assert "Power Plant 5A" in result
    assert "engineer" not in result


def test_status_generator_uses_spec_name(tmp_path: Path):
    plugin = _plugin(tmp_path)
    plugin.import_plan(
        "Python",
        "PvE",
        {"steps": [{"id": "pp", "item": "int_powerplant_size5_class5"}]},
    )
    plugin.start_session("PvE", "SHIP-1", "Mina")

    entries = plugin._status_generator({})

    assert len(entries) == 1
    label, content = entries[0]
    assert label == "Ship upgrade"
    assert "Power Plant 5A" in content
    assert "Mina" in content


def _kestrel_armour_plan(plugin: ShipUpgradeManagerPlugin) -> None:
    plugin.import_plan(
        "smallcombat01_nx",
        "Imported Kestrel Mk II",
        {
            "steps": [
                {
                    "id": "module_0",
                    "slot": "Armour",
                    "item": "smallcombat01_nx_armour_reactive",
                    "engineering": {
                        "BlueprintName": "Armour_Thermic",
                        "Level": 5,
                        "ExperimentalEffect": "special_armour_chunky",
                    },
                },
                {
                    "id": "module_1",
                    "slot": "PowerPlant",
                    "item": "Int_Powerplant_Size5_Class5",
                    "engineering": {
                        "BlueprintName": "PowerPlant_Armoured",
                        "Level": 5,
                        "ExperimentalEffect": "special_powerplant_cooled",
                    },
                },
            ]
        },
    )


def _kestrel_loadout(armour_level: int = 2) -> dict:
    return {
        "event": "Loadout",
        "Ship": "smallcombat01_nx",
        "ShipID": 15,
        "Modules": [
            {
                "Slot": "Armour",
                "Item": "smallcombat01_nx_armour_reactive",
                "Engineering": {"BlueprintName": "Armour_Thermic", "Level": armour_level, "Quality": 0.5},
            },
            {"Slot": "PowerPlant", "Item": "int_powerplant_size5_class5"},
        ],
    }


def _armour_craft(level: int, apply_effect: bool = False) -> dict:
    craft = {
        "event": "EngineerCraft",
        "Slot": "Armour",
        "Module": "smallcombat01_nx_armour_reactive",
        "Engineer": "Selene Jean",
        "EngineerID": 300210,
        "BlueprintName": "Armour_Thermic",
        "Level": level,
        "Quality": 1.0,
        "ExperimentalEffect": "special_armour_chunky",
        "ExperimentalEffect_Localised": "Deep Plating",
    }
    if apply_effect:
        craft["ApplyExperimentalEffect"] = "special_armour_chunky"
    return craft


def test_engineer_craft_without_following_loadout_completes_step(tmp_path: Path):
    """The game writes no Loadout after crafting: EngineerCraft must count."""
    plugin = _plugin(tmp_path)
    _kestrel_armour_plan(plugin)
    plugin.start_session("Imported Kestrel Mk II", "15")
    plugin._on_event(GameEvent(content=_kestrel_loadout(2), historic=False), {})
    assert plugin.get_session()["completed_steps"] == []

    plugin._on_event(GameEvent(content=_armour_craft(2, apply_effect=True), historic=False), {})
    plugin._on_event(GameEvent(content=_armour_craft(4), historic=False), {})
    assert plugin.get_session()["completed_steps"] == []

    plugin._on_event(GameEvent(content=_armour_craft(5), historic=False), {})
    assert plugin.get_session()["completed_steps"] == ["module_0"]
    armour = next(m for m in plugin._current_loadout_modules if m["Slot"] == "Armour")
    assert armour["Engineering"]["Level"] == 5
    assert armour["Engineering"]["ExperimentalEffect"] == "special_armour_chunky"
    assert "Power Plant" in plugin._next_step_action()


def test_journal_baseline_applies_engineer_crafts_after_last_loadout(
    tmp_path: Path, monkeypatch
):
    import plugins.ship_upgrade_manager.ship_upgrade_manager as sum_module

    journal_dir = tmp_path / "journals"
    journal_dir.mkdir()
    lines = [
        _kestrel_loadout(2),
        _armour_craft(2, apply_effect=True),
        _armour_craft(5),
    ]
    (journal_dir / "Journal.2026-09-29T191357.01.log").write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8"
    )
    monkeypatch.setattr(sum_module, "get_ed_journals_path", lambda _config: str(journal_dir))

    plugin = _plugin(tmp_path)
    _kestrel_armour_plan(plugin)
    session = plugin.start_session("Imported Kestrel Mk II", "15")

    assert session["completed_steps"] == ["module_0"]


def test_journal_baseline_ignores_crafts_made_on_another_ship(
    tmp_path: Path, monkeypatch
):
    import plugins.ship_upgrade_manager.ship_upgrade_manager as sum_module

    journal_dir = tmp_path / "journals"
    journal_dir.mkdir()
    other_ship = {**_kestrel_loadout(2), "ShipID": 99}
    lines = [_kestrel_loadout(2), other_ship, _armour_craft(5)]
    (journal_dir / "Journal.2026-09-29T191357.01.log").write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8"
    )
    monkeypatch.setattr(sum_module, "get_ed_journals_path", lambda _config: str(journal_dir))

    plugin = _plugin(tmp_path)
    _kestrel_armour_plan(plugin)

    assert plugin.start_session("Imported Kestrel Mk II", "15")["completed_steps"] == []


def test_baseline_requires_planned_experimental_effect(tmp_path: Path):
    plugin = _plugin(tmp_path)
    step = {
        "id": "armour",
        "item": "smallcombat01_nx_armour_reactive",
        "engineering": {
            "BlueprintName": "Armour_Thermic",
            "Level": 5,
            "ExperimentalEffect": "special_armour_chunky",
        },
    }
    without_effect = [{
        "Slot": "Armour",
        "Item": "smallcombat01_nx_armour_reactive",
        "Engineering": {"BlueprintName": "Armour_Thermic", "Level": 5},
    }]
    with_effect = [{
        "Slot": "Armour",
        "Item": "smallcombat01_nx_armour_reactive",
        "Engineering": {
            "BlueprintName": "Armour_Thermic",
            "Level": 5,
            "ExperimentalEffect": "special_armour_chunky",
        },
    }]

    assert plugin._steps_satisfied_by_loadout([step], without_effect) == []
    assert plugin._steps_satisfied_by_loadout([step], with_effect) == ["armour"]


def test_module_info_keeps_known_engineering(tmp_path: Path):
    plugin = _plugin(tmp_path)
    plugin._on_event(GameEvent(content=_kestrel_loadout(5), historic=False), {})
    plugin._on_event(
        GameEvent(
            content={
                "event": "ModuleInfo",
                "Modules": [
                    {"Slot": "Armour", "Item": "smallcombat01_nx_armour_reactive"},
                    {"Slot": "PowerPlant", "Item": "int_powerplant_size5_class5"},
                ],
            },
            historic=False,
        ),
        {},
    )
    armour = next(m for m in plugin._current_loadout_modules if m["Slot"] == "Armour")
    assert armour["Engineering"]["Level"] == 5


class _MaterialsEventManager:
    def __init__(self, materials: dict | None):
        self.materials = materials

    def get_current_state(self):
        return [], ({"Materials": self.materials} if self.materials is not None else {})


def _materials_state(raw=None, manufactured=None, encoded=None) -> dict:
    def entries(counts):
        return [{"Name": name, "Count": count} for name, count in (counts or {}).items()]

    return {
        "Raw": entries(raw),
        "Manufactured": entries(manufactured),
        "Encoded": entries(encoded),
        "LastUpdated": "2026-09-29T10:00:00Z",
    }


# Kestrel plan vs armour at Thermic grade 2, Quality 0.5 (Deep Plating
# already applied). Grade N costs N increments of its recipe, so the armour
# needs 1 increment of G2 then 3 x G3, 4 x G4, 5 x G5 (matches the real
# journal EngineerCraft sequence); the power plant needs G1..G5 in full plus
# Thermal Spread. 20 material types; with this stock 19 are missing
# (84 units): Vanadium 9, Tungsten 9, then 5 each of Carbon, Compound
# Shielding, Core Dynamics Composites, Grid Resistors, Heat Vanes, ...
_KESTREL_STOCK = {"raw": {"vanadium": 1}, "manufactured": {"heatexchangers": 30}}


def _kestrel_materials_plugin(
    tmp_path: Path, materials: dict | None, deep_plating_applied: bool = True
) -> ShipUpgradeManagerPlugin:
    plugin = _plugin(tmp_path)
    _kestrel_armour_plan(plugin)
    plugin.start_session("Imported Kestrel Mk II", "15")
    loadout = _kestrel_loadout(2)
    if deep_plating_applied:
        loadout["Modules"][0]["Engineering"]["ExperimentalEffect"] = "special_armour_chunky"
    plugin._on_event(GameEvent(content=loadout, historic=False), {})
    plugin.helper._event_manager = _MaterialsEventManager(materials)
    return plugin


def test_missing_materials_summary_counts_types_and_gives_top_three_only(tmp_path: Path):
    from plugins.ship_upgrade_manager.ship_upgrade_manager import MissingMaterialsParams

    plugin = _kestrel_materials_plugin(tmp_path, _materials_state(**_KESTREL_STOCK))

    reply = plugin._missing_materials_action(MissingMaterialsParams(), {})

    assert "19 material types still need to be collected" in reply
    assert "(84 units in total)" in reply
    assert "Vanadium: 9 missing; Tungsten: 9 missing; Carbon: 5 missing." in reply
    assert "Grid Resistors" not in reply
    assert "Heat Exchangers" not in reply


def test_top_missing_materials_lists_count_with_collection_plan(tmp_path: Path):
    from plugins.ship_upgrade_manager.ship_upgrade_manager import MissingMaterialsParams

    plugin = _kestrel_materials_plugin(tmp_path, _materials_state(**_KESTREL_STOCK))

    reply = plugin._missing_materials_action(MissingMaterialsParams(count=7), {})

    assert "The 7 most missing materials" in reply
    assert "1. Vanadium (Raw grade 2): missing 9 (need 10, have 1)" in reply
    assert "2. Tungsten" in reply
    assert "6. Grid Resistors (Manufactured grade 1, family Capacitors): missing 5" in reply
    # Only surplus stock beyond the plan's own need is traded (30 - 3),
    # 6 Heat Exchangers per Heat Vane; the rest must be collected.
    assert (
        "7. Heat Vanes (Manufactured grade 4, family Heat): missing 5 (need 5, have 0); "
        "trade surplus stock at a manufactured material trader: "
        "24 Heat Exchangers (grade 3) for 4; collect 1 more from"
    ) in reply
    assert "8." not in reply


def test_material_shortfall_reports_quantity_for_named_material(tmp_path: Path):
    from plugins.ship_upgrade_manager.ship_upgrade_manager import MaterialQueryParams

    plugin = _kestrel_materials_plugin(tmp_path, _materials_state(**_KESTREL_STOCK))

    reply = plugin._material_shortfall_action(MaterialQueryParams(material="vanadium"), {})
    assert "Vanadium (Raw grade 2): missing 9 (need 10, have 1)" in reply
    assert "Power Plant 5A" in reply

    fuzzy = plugin._material_shortfall_action(MaterialQueryParams(material="heat vane"), {})
    assert "Heat Vanes" in fuzzy and "missing 5" in fuzzy

    enough = plugin._material_shortfall_action(MaterialQueryParams(material="Heat Exchangers"), {})
    assert "You have enough Heat Exchangers" in enough

    unused = plugin._material_shortfall_action(MaterialQueryParams(material="Iron"), {})
    assert "Iron is not needed by the remaining upgrades" in unused


def test_next_step_warns_automatically_about_missing_materials(tmp_path: Path):
    plugin = _kestrel_materials_plugin(tmp_path, _materials_state(**_KESTREL_STOCK))

    reply = plugin._next_step_action()

    assert "Reactive Surface Composite" in reply
    assert "Warning: materials are missing for this upgrade, 10 types" in reply
    assert "Molybdenum (missing 5)" in reply
    assert "and 7 more" in reply


def test_next_step_confirms_materials_when_stock_is_sufficient(tmp_path: Path):
    # Exactly the ingredients of the journal sequence G2 (Quality 0.5) -> G5.
    stock = {
        "raw": {"nickel": 1, "vanadium": 3, "tungsten": 4, "molybdenum": 5},
        "manufactured": {
            "heatdispersionplate": 1, "heatexchangers": 3, "salvagedalloys": 3,
            "galvanisingalloys": 4, "heatvanes": 4, "phasealloys": 5,
            "protoheatradiators": 5,
        },
    }
    plugin = _kestrel_materials_plugin(tmp_path, _materials_state(**stock))

    reply = plugin._next_step_action()

    assert "you have everything needed for this engineering" in reply

    stock["raw"]["molybdenum"] = 4
    plugin.helper._event_manager = _MaterialsEventManager(_materials_state(**stock))
    assert "Molybdenum (missing 1)" in plugin._next_step_action()


def test_next_step_reports_unchecked_stock_without_inventory(tmp_path: Path):
    plugin = _kestrel_materials_plugin(tmp_path, None)

    reply = plugin._next_step_action()

    assert "Material stock could not be checked" in reply


def test_step_materials_details_for_named_module(tmp_path: Path):
    from plugins.ship_upgrade_manager.ship_upgrade_manager import StepMaterialsParams

    plugin = _kestrel_materials_plugin(tmp_path, _materials_state(**_KESTREL_STOCK))

    reply = plugin._step_materials_action(StepMaterialsParams(module="power plant"), {})

    assert "Materials for Power Plant 5A" in reply
    assert "Grid Resistors: need 5, have 0, missing 5" in reply
    assert "Vanadium: need 7, have 1, missing 6" in reply
    assert "Proprietary Composites: need 4" in reply
    assert "12 material types are missing." in reply

    next_reply = plugin._step_materials_action(StepMaterialsParams(), {})
    assert "Materials for Reactive Surface Composite" in next_reply
    assert "Heat Exchangers: need 3, have 30." in next_reply
    assert "Vanadium: need 3, have 1, missing 2" in next_reply
    # The unfinished grade 2 increment (Quality 0.5) is counted.
    assert "Nickel: need 1, have 0, missing 1" in next_reply
    assert "Compact Composites" not in next_reply


def test_step_materials_counts_experimental_effect_not_yet_applied(tmp_path: Path):
    from plugins.ship_upgrade_manager.ship_upgrade_manager import StepMaterialsParams

    plugin = _kestrel_materials_plugin(
        tmp_path, _materials_state(**_KESTREL_STOCK), deep_plating_applied=False
    )

    reply = plugin._step_materials_action(StepMaterialsParams(), {})

    # Deep Plating is applied once: 5 Compact Composites, 3 Mechanical
    # Equipment, 2 Molybdenum (+5 Molybdenum for the 5 grade-5 increments).
    assert "Compact Composites: need 5, have 0, missing 5" in reply
    assert "Mechanical Equipment: need 3, have 0, missing 3" in reply
    assert "Molybdenum: need 7, have 0, missing 7" in reply


def test_incomplete_target_grade_does_not_complete_step(tmp_path: Path):
    """G5 reached at Quality 0.8 has no max effect yet: the step stays open,
    progress counts completed grades only and one G5 increment is needed."""
    from plugins.ship_upgrade_manager.ship_upgrade_manager import StepMaterialsParams

    plugin = _kestrel_materials_plugin(tmp_path, _materials_state(**_KESTREL_STOCK))
    partial = {**_armour_craft(5), "Quality": 0.8}
    plugin._on_event(GameEvent(content=partial, historic=False), {})

    assert plugin.get_session()["completed_steps"] == []
    armour_step = plugin.get_session()["steps"][0]
    assert plugin._steps_satisfied_by_loadout([armour_step], plugin._current_loadout_modules) == []
    assert plugin._progress_metrics([armour_step], plugin._current_loadout_modules)["eng_current"] == 4

    reply = plugin._step_materials_action(StepMaterialsParams(), {})
    assert "Phase Alloys: need 1" in reply
    assert "Molybdenum: need 1" in reply
    assert "Heat Vanes" not in reply

    plugin._on_event(GameEvent(content=_armour_craft(5), historic=False), {})
    assert plugin.get_session()["completed_steps"] == ["module_0"]


def test_build_advice_marks_grades_with_missing_materials(tmp_path: Path):
    plugin = _kestrel_materials_plugin(tmp_path, _materials_state(**_KESTREL_STOCK))

    reply = plugin._build_advice_action(BuildAdviceParams(detailed=True))

    assert "Thermal resistant grade 2 (materials missing beyond grade 2, increment 1)" in reply
    assert "Thermal resistant grade 5 (5 increments) (materials missing beyond grade 2, increment 1)" in reply
    assert "Materials: 19 material types are missing (84 units)" in reply


def test_build_advice_route_adds_trader_stop_and_checks_engineer_progress(tmp_path: Path):
    plugin = _kestrel_materials_plugin(tmp_path, _materials_state(**_KESTREL_STOCK))
    materials = _materials_state(**_KESTREL_STOCK)

    class EventManager:
        def get_current_state(self):
            return [], {
                "Materials": materials,
                "EngineerProgress": {"Engineers": [
                    {"Engineer": "Etienne Dorn", "Progress": "Unlocked", "Rank": 5},
                    {"Engineer": "Petra Olmanova", "Progress": "Unlocked", "Rank": 5},
                ]},
            }

    searched = []

    def search_material_traders(category, *, cache=None):
        searched.append((category, cache is not None))
        return {"status": "ok", "stations": [{
            "id": "near::trader", "name": "Near Trader", "system": "Near",
            "distance_ly": 1.0, "coords": {"x": 1, "y": 0, "z": 0},
        }]}

    plugin.helper._event_manager = EventManager()
    plugin.helper.search_material_traders = search_material_traders
    plugin._current_position = (0.0, 0.0, 0.0)

    reply = plugin._build_advice_action(BuildAdviceParams(detailed=True))

    assert searched == [("Manufactured", True)]
    assert "Near / Near Trader: Manufactured material trader" in reply
    assert "trade 24 Heat Exchangers for 4 Heat Vanes, 1 Heat Exchangers for 3 Heat Dispersion Plate" in reply
    assert "Etienne Dorn" in reply
    assert "Petra Olmanova" in reply
    assert "not unlocked yet" not in reply
    assert "Engineer unlock status is unknown" not in reply
    assert "ly from previous stop" in reply


def test_build_advice_mentions_unknown_engineer_progress(tmp_path: Path):
    plugin = _kestrel_materials_plugin(tmp_path, _materials_state(**_KESTREL_STOCK))

    reply = plugin._build_advice_action()

    assert "Engineer unlock status is unknown" in reply


def test_material_plan_trades_down_surplus_and_flags_storage_cap():
    from plugins.ship_upgrade_manager.materials import (
        MaterialReference,
        MaterialTotal,
        acquisition_plan,
        inventory_from_state,
        shortfalls,
    )

    reference = MaterialReference(
        {"Raw": [{"Name": "carbon"}, {"Name": "vanadium"}]},
        {"carbon": "Carbon", "vanadium": "Vanadium"},
        {"Raw": {1: {1: ["carbon"], 2: ["vanadium"], "source": "Somewhere"}}},
    )
    totals = {"carbon": MaterialTotal("carbon", 400, ["pp"])}
    inventory = {"vanadium": 10}

    [row] = shortfalls(totals, inventory, reference)
    plan = acquisition_plan(row, inventory, totals, reference)

    assert "10 Vanadium (grade 2) for 30" in plan
    assert "collect 370 more from known farming spot for its family: Somewhere" in plan
    assert "storage is capped at 300" in plan
    assert inventory_from_state({"Raw": [{"Name": "carbon", "Count": 0}]}) is None


@pytest.mark.parametrize(
    "session_id", ["Kestrel Mk II", "15", "smallcombat01_nx", "Imported Kestrel Mk II", "current"]
)
def test_ai_session_labels_resolve_to_active_session(tmp_path: Path, session_id: str):
    """The AI passes plan/ship labels, never the internal session hash."""
    plugin = _plugin(tmp_path)
    _kestrel_armour_plan(plugin)
    plugin.start_session("Imported Kestrel Mk II", "15")

    reply = plugin._next_step_action(session_id)

    assert "no active" not in reply.lower()
    assert "1 of 2" in reply


def test_state_query_resolves_ship_model(tmp_path: Path):
    plugin = _plugin(tmp_path)
    _kestrel_armour_plan(plugin)
    plugin.start_session("Imported Kestrel Mk II", "15")

    class Args:
        ship = "smallcombat01_nx"

    reply = plugin._state_query_action(Args(), {})

    assert "No active" not in reply
    assert "Imported Kestrel Mk II" in reply


def test_session_label_picks_matching_ship_among_several(tmp_path: Path):
    plugin = _plugin(tmp_path)
    plugin.import_plan("Python", "Mining", {"steps": [{"id": "laser"}]})
    plugin.import_plan("Vulture", "Combat", {"steps": [{"id": "shield"}]})
    mining = plugin.start_session("Mining", "SHIP-1", "Digger")
    combat = plugin.start_session("Combat", "SHIP-2", "Hawk")

    assert plugin.get_session("Digger")["id"] == mining["id"]
    assert plugin.get_session("combat")["id"] == combat["id"]
    assert plugin.get_session("unknown ship") is None


def test_goal_action_keeps_existing_session_progress(tmp_path: Path):
    plugin = _plugin(tmp_path)
    _kestrel_armour_plan(plugin)
    plugin.start_session("Imported Kestrel Mk II", "15")
    plugin.complete_step("module_0")
    plugin._current_ship_id = "15"

    class Args:
        plan_name = "Imported Kestrel Mk II"

    reply = plugin._goal_declare_action(Args(), {})

    assert "already active" in reply
    assert plugin.get_session()["completed_steps"] == ["module_0"]


def test_build_advice_works_from_imported_plan_without_active_session(tmp_path: Path):
    plugin = _plugin(tmp_path)
    plugin.helper = None
    plugin._current_ship_id = "SHIP-1"
    plugin._current_loadout_modules = []
    plugin.import_plan(
        "Python",
        "Target build",
        {"steps": [{"id": "pp", "item": "int_powerplant_size5_class5"}]},
    )

    reply = plugin._build_advice_action()

    assert reply
    assert "There is no active ship upgrade session" not in reply
    assert "No target build is imported yet" not in reply
    assert "route" in reply.lower() or "cannot compute" in reply.lower() or "nothing pending" in reply.lower()


def test_build_advice_without_session_or_imported_plan_explains_next_step(tmp_path: Path):
    plugin = _plugin(tmp_path)

    reply = plugin._build_advice_action()

    assert reply == "No target build is imported yet. Import a target build in the Plans settings first."


def test_route_request_without_ship_identity_does_not_claim_or_save_a_route(tmp_path: Path):
    plugin = _plugin(tmp_path)
    plugin.import_plan("python", "Target build", {"steps": [
        {"id": "fsd", "item": "int_hyperdrive_size4_class5"},
    ]})

    reply = plugin._build_advice_action()

    assert "cannot identify the current ship" in reply
    assert plugin._route_records == {}
    assert plugin._field("route", "route_empty")["content"] == "plugin.sum.route.empty"
    with plugin.get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM upgrade_routes").fetchone()[0] == 0


@pytest.mark.parametrize("tracking", [False, True])
def test_prompt_distinguishes_remaining_upgrades_from_calculated_routes(
    tmp_path: Path, tracking: bool,
) -> None:
    plugin = _plugin(tmp_path)
    plugin.import_plan("python", "Target build", {"steps": [
        {"id": "fsd", "slot": "FrameShiftDrive", "item": "int_hyperdrive_size4_class5",
         "engineering": {"BlueprintName": "FSD_LongRange", "Level": 3}},
    ]})
    plugin._handle_ship_event({"event": "LoadGame", "FID": "F1", "ShipID": 1, "Ship": "python"})
    plugin._handle_ship_event({"event": "Loadout", "ShipID": 1, "Ship": "python", "Modules": [
        {"Slot": "FrameShiftDrive", "Item": "int_hyperdrive_size4_class5"},
    ]})
    if tracking:
        plugin.start_session("Target build", "1")
    before = plugin._status_generator({})[0][1]
    assert "No upgrade route has been calculated for this ship" in before
    assert "overview is not an itinerary" in before
    assert "ship_upgrade_build_advice, not web_search_agent or showUI" in before
    assert "Never claim the route is ready without a successful calculation result" in before

    plugin._build_advice_action()

    after = plugin._status_generator({})[0][1]
    assert "No upgrade route has been calculated" not in after
    assert "Saved route status:" in after
    assert "Recalculate only on explicit request using ship_upgrade_build_advice" in after


def test_status_generator_exposes_imported_target_without_active_session(tmp_path: Path):
    plugin = _plugin(tmp_path)
    plugin.import_plan(
        "Python",
        "Target Build",
        {"steps": [{"id": "pp", "item": "int_powerplant_size5_class5"}]},
    )
    plugin._current_ship_id = "SHIP-7"
    plugin._current_loadout_modules = [
        {"Slot": "PowerPlant", "Item": "int_powerplant_size5_class5"}
    ]

    status = plugin._status_generator({})

    assert len(status) == 1
    assert status[0][0] == "Ship upgrade target"
    assert "Target Build" in status[0][1]
    assert "no progress session has been activated" in status[0][1]
    assert "1/1 target modules (100%)" in status[0][1]
    assert "All 1 target modules are installed." in status[0][1]


def _missing_modules_plugin(tmp_path: Path, installed: list[dict]) -> ShipUpgradeManagerPlugin:
    plugin = _plugin(tmp_path)
    plugin.import_plan(
        "Python",
        "Target Build",
        {
            "steps": [
                {"id": "mrp", "item": "int_guardianmodulereinforcement_size2_class1", "slot": "Slot07_Size2"},
                {"id": "cargo", "item": "int_cargorack_size4_class1", "slot": "Slot04_Size4"},
                {"id": "booster", "item": "hpt_shieldbooster_size0_class5", "slot": "TinyHardpoint1"},
                {"id": "laser", "item": "hpt_pulselaser_fixed_medium", "slot": "MediumHardpoint1"},
                {"id": "shield", "item": "int_shieldgenerator_size6_class5", "slot": "Slot01_Size6"},
                {"id": "pp", "item": "int_powerplant_size6_class5", "slot": "PowerPlant"},
            ]
        },
    )
    plugin._current_ship_id = "SHIP-7"
    plugin._current_loadout_modules = installed
    return plugin


_ALL_BUT_MRP = [
    {"Slot": "Slot04_Size4", "Item": "int_cargorack_size4_class1"},
    {"Slot": "TinyHardpoint1", "Item": "hpt_shieldbooster_size0_class5"},
    {"Slot": "MediumHardpoint1", "Item": "hpt_pulselaser_fixed_medium"},
    {"Slot": "Slot01_Size6", "Item": "int_shieldgenerator_size6_class5"},
    {"Slot": "PowerPlant", "Item": "int_powerplant_size6_class5"},
]


def test_missing_modules_are_named_when_few(tmp_path: Path):
    plugin = _missing_modules_plugin(tmp_path, _ALL_BUT_MRP)

    reply = plugin._missing_modules_action(MissingModulesParams(), {})
    status = plugin._status_generator({})[0][1]

    assert "1 of 6 target modules are not installed yet: Guardian Module Reinforcement 2E." in reply
    assert "Technology Broker unlock status: unknown" in reply
    assert "5/6 target modules" in status
    assert "not installed yet: Guardian Module Reinforcement 2E" in status
    plugin._current_fid = "F1"
    plugin._broker_set_status("guardian_module_reinforcement_package", "unlocked")
    assert "Technology Broker unlock status: unlocked" in plugin._missing_modules_action(MissingModulesParams(), {})


@pytest.mark.parametrize("ship_type", ["python", "smallcombat01_nx", ""])
def test_named_plan_broker_modules_do_not_depend_on_current_ship(tmp_path, ship_type):
    plugin = _plugin(tmp_path)
    plugin.import_plan("smallcombat01_nx", "Kestrel combat", {"steps": [
        {"id": "mrp", "item": "int_guardianmodulereinforcement_size2_class1", "slot": "Slot07_Size2"},
        {"id": "pp", "item": "int_powerplant_size5_class5", "slot": "PowerPlant"},
    ]})
    plugin._current_ship_type = ship_type
    plugin._current_ship_id = "python-7" if ship_type else ""
    plugin._current_loadout_modules = []
    plugin._loadout_for_ship = Mock(side_effect=AssertionError("Plan recall must not read a live loadout"))

    report = json.loads(plugin._broker_query_action(BrokerQueryParams(plan_name="Kestrel"), {}))

    assert report["status"] == "ok"
    assert report["plan"] == "Kestrel combat"
    assert len(report["modules"]) == 1
    assert report["modules"][0]["module"] == "Guardian Module Reinforcement 2E"
    assert report["modules"][0]["item"] == "int_guardianmodulereinforcement_size2_class1"
    assert report["modules"][0]["unlock_status"] == "unknown"
    plugin._loadout_for_ship.assert_not_called()


def test_broker_plan_query_reports_ambiguous_and_unknown_plans(tmp_path):
    plugin = _plugin(tmp_path)
    for name in ("Kestrel combat", "Kestrel exploration"):
        plugin.import_plan("smallcombat01_nx", name, {"steps": [
            {"id": "mrp", "item": "int_guardianmodulereinforcement_size2_class1"},
        ]})

    ambiguous = json.loads(plugin._broker_query_action(BrokerQueryParams(plan_name="Kestrel"), {}))
    assert ambiguous["status"] == "ambiguous_or_unknown_plan"
    assert set(ambiguous["matching_plans"]) == {"Kestrel combat", "Kestrel exploration"}
    unknown = json.loads(plugin._broker_query_action(BrokerQueryParams(plan_name="Unknown"), {}))
    assert unknown["status"] == "ambiguous_or_unknown_plan"
    assert unknown["matching_plans"] == []
    exact = json.loads(plugin._broker_query_action(BrokerQueryParams(plan_name="Kestrel combat"), {}))
    assert exact["plan"] == "Kestrel combat"


def test_broker_plan_query_reports_no_broker_modules(tmp_path):
    plugin = _plugin(tmp_path)
    plugin.import_plan("python", "Ordinary modules", {"steps": [
        {"id": "pp", "item": "int_powerplant_size5_class5"},
    ]})

    report = json.loads(plugin._broker_query_action(BrokerQueryParams(plan_name="Ordinary modules"), {}))

    assert report["status"] == "ok"
    assert report["modules"] == []
    assert "contains no matching broker module" in report["response_policy"]


def test_unrelated_current_ship_is_not_compared_to_imported_plan(tmp_path):
    plugin = _missing_modules_plugin(tmp_path, [])
    plugin._current_ship_type = "asp"
    plugin._loadout_for_ship = Mock(side_effect=AssertionError("Wrong ship comparison"))

    result = plugin._missing_modules_action(MissingModulesParams(), {})
    status = plugin._status_generator({})[0][1]

    assert "Cannot compare this plan to a different ship's loadout" in result
    assert "plan_name=" in result
    assert "Do not compare this plan to the current ship's loadout" in status
    assert "target modules are not installed" not in status
    plugin._loadout_for_ship.assert_not_called()


@pytest.mark.parametrize("multiple_products", [False, True])
def test_named_plan_broker_station_search_is_explicit_and_unambiguous(tmp_path, multiple_products):
    plugin = _plugin(tmp_path)
    steps = [{"id": "mrp", "item": "int_guardianmodulereinforcement_size2_class1"}]
    if multiple_products:
        steps.append({"id": "fsd", "item": "int_guardianfsdbooster_size5"})
    plugin.import_plan("smallcombat01_nx", "Kestrel", {"steps": steps})
    plugin._broker_station_candidates = Mock(return_value="Synthetic station candidates")

    report = json.loads(plugin._broker_query_action(
        BrokerQueryParams(plan_name="Kestrel", find_stations=True), {},
    ))

    if multiple_products:
        assert "Select one exact broker product" in report["station_search"]
        plugin._broker_station_candidates.assert_not_called()
    else:
        assert "Synthetic station candidates" in report["procurement"]
        plugin._broker_station_candidates.assert_called_once()


def test_selected_broker_target_is_in_context_after_ship_change(tmp_path):
    plugin = _plugin(tmp_path)
    plugin._current_fid = "F1"
    plugin._broker_query_action(BrokerQueryParams(module="Guardian Module Reinforcement Package"), {})
    plugin._current_ship_id = "other-ship"
    plugin._current_ship_type = "python"

    entries = dict(plugin._status_generator({}))
    recalled = plugin._broker_query_action(BrokerQueryParams(), {})

    assert "Guardian Module Reinforcement Package" in entries["Technology Broker target"]
    assert "retained across ship changes" in entries["Technology Broker target"]
    assert "plan_name" in entries["Technology Broker target"]
    assert recalled.startswith("Guardian Module Reinforcement Package:")


def test_many_missing_modules_give_count_and_most_important(tmp_path: Path):
    plugin = _missing_modules_plugin(tmp_path, [])

    reply = plugin._missing_modules_action(MissingModulesParams(), {})

    assert "6 of 6 target modules are not installed yet." in reply
    top = reply.split("The most important to install first: ")[1].split(". ")[0]
    assert top.split("; ")[0].startswith("Power Plant")
    assert top.split("; ")[1].startswith("Shield Generator")
    assert len(top.split("; ")) == 3
    assert "Guardian Module Reinforcement" not in reply
    assert "offer to install these first" in reply

    full = plugin._missing_modules_action(MissingModulesParams(list_all=True), {})
    assert "Guardian Module Reinforcement 2E" in full
    assert full.index("Cargo Rack") < full.index("Guardian Module Reinforcement")


def test_state_query_names_missing_module(tmp_path: Path):
    plugin = _missing_modules_plugin(tmp_path, _ALL_BUT_MRP)
    plugin.start_session("Target Build", "SHIP-7")

    reply = plugin._state_query_action(StateQueryParams(), {})

    assert "5/6 modules installed" in reply
    assert "not installed yet: Guardian Module Reinforcement 2E" in reply


def test_remaining_upgrades_prioritizes_three_from_live_loadout_despite_stale_completion(tmp_path: Path):
    plugin = _plugin(tmp_path)
    steps = [
        {"id": "life", "slot": "LifeSupport", "item": "int_lifesupport_size1_class2", "engineering": {"BlueprintName": "Misc_LightWeight", "Level": 5}},
        {"id": "mrp", "slot": "Slot05_Size2", "item": "int_guardianmodulereinforcement_size2_class1"},
        {"id": "pp", "slot": "PowerPlant", "item": "int_powerplant_size5_class5", "engineering": {"BlueprintName": "PowerPlant_Armoured", "Level": 5, "ExperimentalEffect": "special_powerplant_cooled"}},
        {"id": "pd", "slot": "PowerDistributor", "item": "int_powerdistributor_size5_class5", "engineering": {"BlueprintName": "PowerDistributor_HighFrequency", "Level": 5}},
        {"id": "shield", "slot": "Slot01_Size5", "item": "int_shieldgenerator_size5_class3_fast", "engineering": {"BlueprintName": "ShieldGenerator_Thermic", "Level": 5}},
        {"id": "fsd", "slot": "FrameShiftDrive", "item": "int_hyperdrive_size4_class5", "engineering": {"BlueprintName": "FSD_LongRange", "Level": 5}},
    ]
    plugin.import_plan("Python", "Combat", {"steps": steps})
    plugin._current_ship_id = "15"
    plugin._current_loadout_modules = [
        {"Slot": step["slot"], "Item": step["item"], "Engineering": {**step["engineering"], "Level": 3, "Quality": 1}}
        for step in steps if step["id"] != "mrp"
    ]
    plugin._current_loadout_modules[1]["Engineering"] = {"BlueprintName": "PowerPlant_Armoured", "Level": 5, "Quality": .8}
    plugin._current_loadout_modules[-1]["Engineering"]["Level"] = 5
    session = plugin.start_session("Combat", "15")
    plugin.complete_step("pp")
    before = plugin.get_session(session["id"])

    report = json.loads(plugin._remaining_upgrades_action(RemainingUpgradesParams(), {}))
    assert report["remaining_module_count"] == 5
    assert report["omitted_module_count"] == 2
    assert [row["target_slot"] for row in report["modules"]] == ["PowerPlant", "PowerDistributor", "LifeSupport"]
    pp = report["modules"][0]
    assert pp["current_quality"] == .8
    assert pp["target_experimental"] == "Thermal Spread"
    assert pp["remaining_work"] == ["finish_target_grade", "apply_experimental_effect"]
    assert plugin.get_session(session["id"]) == before

    full = json.loads(plugin._remaining_upgrades_action(RemainingUpgradesParams(list_all=True), {}))
    assert full["remaining_module_count"] == 5
    assert len(full["modules"]) == 5
    assert full["omitted_module_count"] == 0
    assert full["modules"][-1]["remaining_work"] == ["install_module"]
    assert all(row["target_slot"] != "FrameShiftDrive" for row in full["modules"])
    assert '"remaining_module_count": 5' in plugin._status_generator({})[0][1]

    plugin.stop_session(session["id"])
    without_session = json.loads(plugin._remaining_upgrades_action(RemainingUpgradesParams(), {}))
    assert without_session == report


@pytest.mark.parametrize("quality,effect,work", [
    (1, "", ["apply_experimental_effect"]),
    (1, "special_powerplant_overloaded", ["apply_experimental_effect"]),
    (.8, "special_powerplant_cooled", ["finish_target_grade"]),
    (1, "special_powerplant_cooled", []),
])
def test_remaining_upgrades_distinguishes_quality_and_effect(tmp_path: Path, quality, effect, work):
    plugin = _plugin(tmp_path)
    step = {"id": "pp", "item": "int_powerplant_size5_class5", "slot": "PowerPlant", "engineering": {"BlueprintName": "PowerPlant_Armoured", "Level": 5, "ExperimentalEffect": "special_powerplant_cooled"}}
    loadout = [{"Item": step["item"], "Slot": "PowerPlant", "Engineering": {"BlueprintName": "PowerPlant_Armoured", "Level": 5, "Quality": quality, "ExperimentalEffect": effect}}]
    report = plugin._remaining_upgrades_report([step], loadout)
    assert report["remaining_module_count"] == bool(work)
    if work:
        assert report["modules"][0]["remaining_work"] == work


def test_remaining_upgrades_matches_duplicate_modules_one_to_one_across_swapped_slots(tmp_path: Path):
    plugin = _plugin(tmp_path)
    steps = [{"id": str(i), "slot": f"SmallHardpoint{i}", "item": "hpt_slugshot_gimbal_small", "engineering": {"BlueprintName": "Weapon_DoubleShot", "Level": 5}} for i in (1, 2)]
    modules = [{"Slot": "SmallHardpoint3", "Item": steps[0]["item"], "Engineering": {"BlueprintName": "Weapon_DoubleShot", "Level": 5, "Quality": 1}}]
    report = plugin._remaining_upgrades_report(steps, modules, list_all=True)
    assert report["remaining_module_count"] == 1
    assert report["modules"][0]["installed"] is False


def test_remaining_upgrades_reports_unknown_ship_and_loadout(tmp_path: Path):
    plugin = _plugin(tmp_path)
    plugin.import_plan("Python", "Target", {"steps": [{"id": "pp", "item": "int_powerplant_size5_class5"}]})
    assert json.loads(plugin._remaining_upgrades_action(RemainingUpgradesParams(), {}))["status"] == "unknown_current_ship"
    plugin._current_ship_id = "15"
    assert json.loads(plugin._remaining_upgrades_action(RemainingUpgradesParams(), {}))["status"] == "unknown_loadout"


def test_remaining_upgrades_rejects_ambiguous_ship_label(tmp_path: Path):
    plugin = _plugin(tmp_path)
    plugin.import_plan("Python", "Target", {"steps": [{"id": "pp", "item": "int_powerplant_size5_class5"}]})
    plugin.start_session("Target", "15")
    other = plugin.start_session("Target", "16")
    plugin._current_ship_id = "15"
    plugin._current_loadout_modules = [{"Slot": "PowerPlant", "Item": "int_powerplant_size5_class5"}]
    ambiguous = json.loads(plugin._remaining_upgrades_action(RemainingUpgradesParams(session_id="Python"), {}))
    assert ambiguous["status"] == "ambiguous_or_unknown_session"
    unknown = json.loads(plugin._remaining_upgrades_action(RemainingUpgradesParams(session_id="does-not-exist"), {}))
    assert unknown["status"] == "ambiguous_or_unknown_session"
    assert json.loads(plugin._remaining_upgrades_action(RemainingUpgradesParams(), {}))["remaining_module_count"] == 0
    assert json.loads(plugin._remaining_upgrades_action(RemainingUpgradesParams(session_id=other["id"]), {}))["status"] == "unknown_loadout"


def test_plan_status_reports_no_loaded_plan(tmp_path: Path):
    plugin = _plugin(tmp_path)

    assert plugin._plan_status_action() == "No ship-upgrade plan is loaded. Import a target build in Plans settings."


def test_plan_status_distinguishes_loaded_plan_from_active_session(tmp_path: Path):
    plugin = _plugin(tmp_path)
    plugin.import_plan("smallcombat01_nx", "Kestrel target", {"steps": [{"id": "pp"}]})

    loaded = plugin._plan_status_action()
    assert "target upgrade plan is loaded but not active" in loaded
    assert "Kestrel target" in loaded
    assert "Kestrel Mk II" in loaded

    plugin.start_session("Kestrel target", "SHIP-15")
    active = plugin._plan_status_action()
    assert "upgrade plan is active and in progress" in active
    assert "Kestrel target" in active
    assert "Kestrel Mk II" in active
    assert "0/1 tracked steps complete" in active


def test_session_summary_is_published_as_i18n_key(tmp_path: Path):
    plugin = _plugin(tmp_path, running=False)
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


def _game_event(content: dict) -> GameEvent:
    return GameEvent(content=content, historic=False)


def test_current_plan_follows_ship_id_across_ship_swaps(tmp_path: Path):
    """Two ships of the same type share one target build at different
    progress levels; the plan in progress is keyed by ShipID, not type."""
    plugin = _plugin(tmp_path)
    plugin.import_plan(
        "Python", "PvE", {"steps": [{"id": "fsd"}, {"id": "shield"}]}
    )
    plugin._handle_ship_event(
        {"event": "Loadout", "Ship": "python", "ShipID": 1, "ShipName": "Alpha", "Modules": []}
    )
    alpha = plugin.start_session("PvE", "1", "Alpha")
    plugin.complete_step("fsd")

    plugin._handle_ship_event(
        {"event": "ShipyardSwap", "ShipType": "python", "ShipID": 2, "StoreShipID": 1}
    )
    assert plugin._current_ship_id == "2"
    assert plugin._current_ship_name == ""
    # The second Python has no plan yet: the first ship's plan is not reused.
    assert plugin.get_session() is None
    assert plugin._field("session", "session_summary")["content"] == "plugin.sum.noSession"

    beta = plugin.start_session("PvE", "2", "Beta")
    assert beta["id"] != alpha["id"]
    assert plugin.get_session()["completed_steps"] == []

    plugin._handle_ship_event(
        {"event": "ShipyardSwap", "ShipType": "python", "ShipID": 1, "StoreShipID": 2}
    )
    session = plugin.get_session()
    assert session["id"] == alpha["id"]
    assert session["completed_steps"] == ["fsd"]
    summary = plugin._field("session", "session_summary")
    assert summary["params"]["ship"] == "Alpha"
    assert summary["params"]["pct"] == 50

    # A new game session starting in the second ship resumes its own plan.
    plugin._handle_ship_event(
        {"event": "LoadGame", "Ship": "python", "ShipID": 2, "ShipName": "Beta", "FID": "F1"}
    )
    assert plugin.get_session()["id"] == beta["id"]
    assert plugin._state_query_action(
        __import__("types").SimpleNamespace(ship="Python"), {}
    ).startswith("Plan PvE on Beta")


def test_ship_type_or_name_is_never_used_as_ship_key(tmp_path: Path):
    plugin = _plugin(tmp_path)
    plugin.import_plan("Python", "PvE", {"steps": [{"id": "fsd", "item": "fsd_item"}]})
    plugin.start_session("PvE", "1")
    plugin.start_session("PvE", "2")

    # No ShipID: the ship type must not become the current ship key.
    plugin._handle_ship_event(
        {"event": "ModuleStore", "Ship": "python", "ShipName": "Alpha", "StoredItem": "fsd_item"}
    )
    assert plugin._current_ship_id == ""
    assert all(session["completed_steps"] == [] for session in plugin.list_sessions())

    plugin._handle_ship_event({"event": "ModuleBuy", "ShipID": 2, "Ship": "python", "BuyItem": "fsd_item"})
    by_ship = {s["ship_instance_id"]: s["completed_steps"] for s in plugin.list_sessions()}
    assert by_ship == {"1": [], "2": ["fsd"]}


def test_module_info_without_ship_id_applies_to_current_ship_only(tmp_path: Path):
    plugin = _plugin(tmp_path)
    plugin.import_plan("Python", "PvE", {"steps": [{"id": "fsd", "item": "fsd_item"}]})
    plugin.start_session("PvE", "1")
    plugin.start_session("PvE", "2")
    plugin._handle_ship_event({"event": "Loadout", "Ship": "python", "ShipID": 1, "Modules": []})

    plugin._handle_ship_event(
        {"event": "ModuleInfo", "Modules": [{"Slot": "FrameShiftDrive", "Item": "fsd_item"}]}
    )

    by_ship = {s["ship_instance_id"]: s["completed_steps"] for s in plugin.list_sessions()}
    assert by_ship == {"1": ["fsd"], "2": []}


def test_live_loadout_snapshot_is_not_reused_for_another_ship(tmp_path: Path):
    plugin = _plugin(tmp_path)
    modules = [{"Slot": "FrameShiftDrive", "Item": "fsd_item"}]
    plugin._handle_ship_event({"event": "Loadout", "Ship": "python", "ShipID": 1, "Modules": modules})
    assert plugin._loadout_for_ship("1") == modules

    plugin._handle_ship_event({"event": "ModuleBuy", "ShipID": 2, "Ship": "python", "BuyItem": "x"})

    assert plugin._current_ship_id == "2"
    assert plugin._loadout_for_ship("2") == []


def test_set_user_ship_name_updates_the_plan_of_that_ship(tmp_path: Path):
    plugin = _plugin(tmp_path)
    plugin.import_plan("Python", "PvE", {"steps": [{"id": "fsd"}]})
    plugin.start_session("PvE", "1", "Old")
    plugin.start_session("PvE", "2", "Other")

    plugin._handle_ship_event(
        {"event": "SetUserShipName", "Ship": "python", "ShipID": 1, "UserShipName": "New"}
    )

    names = {s["ship_instance_id"]: s["ship_custom_name"] for s in plugin.list_sessions()}
    assert names == {"1": "New", "2": "Other"}


def test_journal_detection_ignores_other_ships_and_suits(tmp_path: Path, monkeypatch):
    import plugins.ship_upgrade_manager.ship_upgrade_manager as sum_module

    journal_dir = tmp_path / "journals"
    journal_dir.mkdir()
    (journal_dir / "Journal.2026-09-20T120000.01.log").write_text(
        "\n".join(
            json.dumps(entry)
            for entry in (
                {"event": "Loadout", "Ship": "python", "ShipID": 7, "ShipName": "Seven"},
                {"event": "LoadGame", "Ship": "TacticalSuit_Class1", "ShipID": 99},
                {"event": "ShipyardTransfer", "ShipType": "anaconda", "ShipID": 8},
                {"event": "ShipyardSell", "ShipType": "cobra", "SellShipID": 9},
            )
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sum_module, "get_ed_journals_path", lambda _config: str(journal_dir))
    plugin = _plugin(tmp_path)

    assert plugin._detect_ship_from_journal() == {"id": "7", "name": "Seven", "type": "python"}


def test_journal_reads_follow_the_configured_journal_path(tmp_path: Path, monkeypatch):
    import lib.Config
    import plugins.ship_upgrade_manager.ship_upgrade_manager as sum_module

    monkeypatch.setattr(sum_module, "get_ed_journals_path", lib.Config.get_ed_journals_path)
    first = tmp_path / "first"
    second = tmp_path / "second"
    for folder, ship_id in ((first, 1), (second, 2)):
        folder.mkdir()
        (folder / "Journal.2026-09-20T120000.01.log").write_text(
            json.dumps({"event": "Loadout", "Ship": "python", "ShipID": ship_id}) + "\n",
            encoding="utf-8",
        )
    plugin = _plugin(tmp_path)

    class _Manager(_PluginManager):
        config = {"ed_journal_path": str(first)}

        def _get_owned_app_config(self, _plugin):
            return dict(self.config)

    manager = _Manager()
    plugin._manager = manager  # type: ignore[assignment]
    assert plugin._detect_ship_from_journal()["id"] == "1"

    # The options are read live: a new path applies without a restart.
    manager.config = {"ed_journal_path": str(second)}
    assert plugin._detect_ship_from_journal()["id"] == "2"


def _thrusters_plan(plugin: ShipUpgradeManagerPlugin) -> None:
    plugin.import_plan(
        "Python",
        "Agile",
        {
            "steps": [
                {
                    "id": "thrusters",
                    "item": "int_engine_size5_class5",
                    "slot": "MainEngines",
                    "engineering": {
                        "BlueprintName": "Engine_Tuned",
                        "Level": 5,
                        "ExperimentalEffect": "special_engine_overloaded",
                    },
                }
            ]
        },
    )


def _load_thrusters(plugin: ShipUpgradeManagerPlugin, engineering: dict | None = None) -> None:
    module = {"Slot": "MainEngines", "Item": "int_engine_size5_class5"}
    if engineering:
        module["Engineering"] = engineering
    plugin._handle_ship_event(
        {"event": "Loadout", "Ship": "python", "ShipID": 1, "Modules": [module]}
    )


def test_next_step_does_not_ask_to_install_an_already_installed_module(tmp_path: Path):
    plugin = _plugin(tmp_path)
    _thrusters_plan(plugin)
    _load_thrusters(plugin)
    plugin.start_session("Agile", "1")

    answer = plugin._next_step_action()

    assert "already installed; remaining: engineer it with" in answer
    assert "grade 5, then apply the experimental effect" in answer
    status = plugin._status_generator({})[0][1]
    assert "already installed; remaining:" in status


def test_next_step_speaks_only_the_missing_engineering(tmp_path: Path):
    plugin = _plugin(tmp_path)
    _thrusters_plan(plugin)
    _load_thrusters(plugin, {"BlueprintName": "Engine_Tuned", "Level": 3, "Quality": 1.0})
    plugin.start_session("Agile", "1")

    answer = plugin._next_step_action()
    assert "from grade 3 to grade 5" in answer
    assert "apply the experimental effect" in answer

    _load_thrusters(
        plugin,
        {
            "BlueprintName": "Engine_Tuned",
            "Level": 5,
            "Quality": 1.0,
            "ExperimentalEffect": "special_engine_overloaded",
        },
    )
    session = plugin.get_session()
    match = plugin._step_loadout_match(session["steps"][0], session["steps"], "1")
    assert "Experimental effect Drag Drives already installed" in plugin._step_voice_label(
        session["steps"][0], match
    )


def test_next_step_still_asks_to_install_a_missing_module(tmp_path: Path):
    plugin = _plugin(tmp_path)
    _thrusters_plan(plugin)
    plugin._handle_ship_event({"event": "Loadout", "Ship": "python", "ShipID": 1, "Modules": []})
    plugin.start_session("Agile", "1")

    answer = plugin._next_step_action()

    assert "already installed" not in answer
    assert ", then engineer it with" in answer
    assert "grade 5, then apply the experimental effect" in answer


def test_reaching_module_target_announces_next_step_once(tmp_path: Path):
    plugin = _plugin(tmp_path)
    plugin.import_plan("python", "Build", {"steps": [
        {"id": "thrusters", "slot": "MainEngines", "item": "int_engine_size5_class5",
         "engineering": {"BlueprintName": "Engine_Tuned", "Level": 5}},
        {"id": "fsd", "slot": "FrameShiftDrive", "item": "int_hyperdrive_size4_class5",
         "engineering": {"BlueprintName": "FSD_LongRange", "Level": 5,
                         "ExperimentalEffect": "special_fsd_fuelcapacity"}},
    ]})
    plugin.start_session("Build", "1")
    loadout = {"event": "Loadout", "Ship": "python", "ShipID": 1,
               "Modules": [{"Slot": "MainEngines", "Item": "int_engine_size5_class5",
                            "Engineering": {"BlueprintName": "Engine_Tuned", "Level": 5, "Quality": 1}}]}

    plugin._handle_ship_event(loadout)
    assert len(plugin.helper.events) == 1
    assert plugin.get_session()["completed_steps"] == ["thrusters"]
    assert "has reached its plan target" in plugin.helper.events[0].plugin_event_content
    assert "Deep Charge" in plugin.helper.events[0].plugin_event_content
    plugin._handle_ship_event(loadout)
    assert len(plugin.helper.events) == 1


def test_historical_completion_does_not_trigger_speech(tmp_path: Path):
    plugin = _plugin(tmp_path)
    plugin.import_plan("python", "Build", {"steps": [
        {"id": "thrusters", "slot": "MainEngines", "item": "int_engine_size5_class5"}
    ]})
    plugin.start_session("Build", "1")
    plugin._on_event(GameEvent(content={
        "event": "Loadout", "Ship": "python", "ShipID": 1,
        "Modules": [{"Slot": "MainEngines", "Item": "int_engine_size5_class5"}],
    }, historic=True), {})
    assert plugin.get_session()["completed_steps"] == ["thrusters"]
    assert plugin.helper.events == []


def test_one_loadout_module_does_not_complete_two_identical_plan_steps(tmp_path: Path):
    plugin = _plugin(tmp_path)
    steps = [
        {"id": "first", "item": "hpt_shieldbooster_size0_class5"},
        {"id": "second", "item": "hpt_shieldbooster_size0_class5"},
    ]
    loadout = [{"Slot": "TinyHardpoint1", "Item": "hpt_shieldbooster_size0_class5"}]
    assert plugin._steps_satisfied_by_loadout(steps, loadout) == ["first"]


def _swapped_weapons():
    plasma = "hpt_mkiiplasmashockautocannon_fixed_large"
    beam = "hpt_beamlaser_gimbal_large"
    steps = [
        {"id": "plasma-1", "slot": "LargeHardpoint1", "item": plasma},
        {"id": "plasma-2", "slot": "LargeHardpoint2", "item": plasma},
        {"id": "beam", "slot": "LargeHardpoint3", "item": beam, "engineering": {
            "BlueprintName": "Weapon_LongRange", "Level": 5,
            "ExperimentalEffect": "special_thermal_vent",
        }},
    ]
    modules = [
        {"Slot": "LargeHardpoint1", "Item": beam, "Engineering": {
            "BlueprintName": "Weapon_LongRange", "Level": 5, "Quality": 0.8,
        }},
        {"Slot": "LargeHardpoint2", "Item": plasma},
        {"Slot": "LargeHardpoint3", "Item": plasma},
    ]
    return steps, modules


def test_swapped_installed_weapons_have_consistent_progress_and_remaining_engineering(tmp_path):
    plugin = _plugin(tmp_path)
    steps, modules = _swapped_weapons()
    matches = plugin._match_steps(steps, modules)
    assert all(match["installed"] for match in matches)
    assert [match["satisfied"] for match in matches] == [True, True, False]
    assert [match["slot"] for match in matches] == [
        "largehardpoint3", "largehardpoint2", "largehardpoint1",
    ]

    report = plugin._remaining_upgrades_report(steps, modules, list_all=True)
    assert report["remaining_module_count"] == 1
    [beam] = report["modules"]
    assert beam["item"] == "hpt_beamlaser_gimbal_large"
    assert beam["module_name_fr"] == "Laser à rayon"
    assert beam["remaining_work"] == ["finish_target_grade", "apply_experimental_effect"]
    assert beam["current_quality"] == 0.8
    assert beam["installed_slot"] == "largehardpoint1"
    notice = report["weapon_slots"]
    assert notice["mismatch_count"] == 2
    assert notice["warning_fr"] == "Certaines armes ne sont pas à l'emplacement prévu."
    assert notice["details_tool"] == "ship_upgrade_weapon_slots"
    assert "weapons" not in notice


def test_weapon_slot_details_are_read_only_and_report_actual_and_expected_locations(tmp_path, monkeypatch):
    plugin = _plugin(tmp_path)
    steps, modules = _swapped_weapons()
    plugin.import_plan("python", "Placements", {"steps": steps})
    plugin._handle_ship_event({"event": "Loadout", "ShipID": 1, "Ship": "python", "Modules": modules})
    plugin.start_session("Placements", "1")
    before = deepcopy(plugin.get_session())
    prompt = plugin._status_generator({})[0][1]
    assert "ship_upgrade_weapon_slots" in prompt
    assert "warning_fr" in prompt
    monkeypatch.setattr(plugin, "_build_advice_action", lambda *_: pytest.fail("Unexpected recalculation"))
    plugin.helper.prepare_navigation = Mock(side_effect=AssertionError("Unexpected navigation"))

    report = json.loads(plugin._weapon_slots_action(SessionActionParams(), {}))

    assert report["status"] == "ok"
    assert report["mismatch_count"] == 2
    by_item = {weapon["item"]: weapon for weapon in report["weapons"]}
    beam = by_item["hpt_beamlaser_gimbal_large"]
    assert beam["module_name_fr"] == "Laser à rayon"
    assert (beam["installed_slot"], beam["target_slot"]) == ("largehardpoint1", "largehardpoint3")
    plasma = by_item["hpt_mkiiplasmashockautocannon_fixed_large"]
    assert (plasma["installed_slot"], plasma["target_slot"]) == ("largehardpoint3", "largehardpoint1")
    assert plugin.get_session() == before
    assert plugin._route_records == {}


def test_weapon_placement_notice_does_not_make_an_otherwise_finished_build_incomplete(tmp_path):
    from plugins.ship_upgrade_manager.planner import analyze_build

    plugin = _plugin(tmp_path)
    steps, modules = _swapped_weapons()
    steps[2].pop("engineering")
    report = plugin._remaining_upgrades_report(steps, modules)

    assert report["remaining_module_count"] == 0
    assert report["weapon_slots"]["mismatch_count"] == 2
    summary = plugin._route_summary([], analyze_build(steps, modules))
    assert "no upgrades remain" in summary
    assert "Some weapons are not in their planned slots." in summary
    assert "ship_upgrade_weapon_slots" in summary
    assert "largehardpoint" not in summary


def test_weapon_slot_mismatches_exclude_utilities_missing_items_and_unspecified_slots(tmp_path):
    from plugins.ship_upgrade_manager.planner import analyze_build

    plugin = _plugin(tmp_path)
    steps, modules = _swapped_weapons()
    for step in steps:
        step.pop("slot")
    steps += [
        {"item": "hpt_shieldbooster_size0_class5", "slot": "TinyHardpoint1"},
        {"item": "hpt_pulselaser_gimbal_large", "slot": "LargeHardpoint4"},
    ]
    modules.append({"Slot": "TinyHardpoint2", "Item": "hpt_shieldbooster_size0_class5"})
    report = plugin._weapon_slot_report(analyze_build(steps, modules), detailed=True)

    assert report["mismatch_count"] == 0
    assert report["weapons"] == []
    assert "warning" not in report


@pytest.mark.parametrize("case,status", [
    ("unknown_ship", "unknown_current_ship"),
    ("unknown_loadout", "unknown_loadout"),
    ("unknown_session", "ambiguous_or_unknown_session"),
    ("other_ship", "unknown_loadout"),
])
def test_weapon_slot_query_reports_missing_context_instead_of_claiming_correct_placement(tmp_path, case, status):
    plugin = _plugin(tmp_path)
    steps, modules = _swapped_weapons()
    plugin.import_plan("python", "Placements", {"steps": steps})
    args = SessionActionParams()
    if case != "unknown_ship":
        plugin._handle_ship_event({"event": "LoadGame", "ShipID": 1, "Ship": "python"})
    if case == "unknown_session":
        args = SessionActionParams(session_id="not-a-session")
    if case == "other_ship":
        session = plugin.start_session("Placements", "2")
        args = SessionActionParams(session_id=session["id"])
        plugin._handle_ship_event({"event": "Loadout", "ShipID": 1, "Ship": "python", "Modules": modules})
    result = json.loads(plugin._weapon_slots_action(args, {}))
    assert result["status"] == status


def test_weapon_slot_details_preserve_pulse_family_instead_of_inventing_a_beam(tmp_path):
    plugin = _plugin(tmp_path)
    steps, modules = _swapped_weapons()
    steps[2]["item"] = modules[0]["Item"] = "hpt_pulselaser_gimbal_large"
    plugin.import_plan("python", "Pulse positions", {"steps": steps})
    plugin._handle_ship_event({"event": "Loadout", "ShipID": 1, "Ship": "python", "Modules": modules})

    report = json.loads(plugin._weapon_slots_action(SessionActionParams(), {}))
    pulse = next(weapon for weapon in report["weapons"] if weapon["item"] == "hpt_pulselaser_gimbal_large")
    assert pulse["module_name_fr"] == "Laser à impulsions"
    assert pulse["target_slot"] == "largehardpoint3"


def test_swapped_weapon_craft_completes_actual_module_without_ignoring_quality_or_effect(tmp_path):
    plugin = _plugin(tmp_path)
    steps, modules = _swapped_weapons()
    plugin.import_plan("python", "Swapped weapons", {"steps": steps})
    plugin.start_session("Swapped weapons", "1")
    plugin._handle_ship_event({"event": "Loadout", "ShipID": 1, "Ship": "python", "Modules": modules})
    assert plugin.get_session()["completed_steps"] == ["plasma-1", "plasma-2"]

    craft = {"event": "EngineerCraft", "Slot": "LargeHardpoint1",
             "Module": "hpt_beamlaser_gimbal_large", "BlueprintName": "Weapon_LongRange",
             "Level": 5, "Quality": 1}
    plugin._handle_ship_event(craft)
    assert plugin.get_session()["completed_steps"] == ["plasma-1", "plasma-2"]
    plugin._handle_ship_event({**craft, "ExperimentalEffect": "special_thermal_vent"})
    assert plugin.get_session()["completed_steps"] == ["plasma-1", "plasma-2", "beam"]


def test_repeated_events_do_not_reuse_an_installed_copy_for_completed_steps(tmp_path):
    plugin = _plugin(tmp_path)
    item = "hpt_shieldbooster_size0_class5"
    steps = [{"id": str(index), "slot": f"TinyHardpoint{index}", "item": item} for index in (1, 2)]
    plugin.import_plan("python", "Two boosters", {"steps": steps})
    plugin.start_session("Two boosters", "1")
    event = {"event": "Loadout", "Ship": "python", "ShipID": 1,
             "Modules": [{"Slot": "TinyHardpoint3", "Item": item}]}

    for _ in range(3):
        plugin._handle_ship_event(event)
        assert plugin.get_session()["completed_steps"] == ["1"]
    plugin._handle_ship_event({"event": "ModuleBuy", "ShipID": 1, "Slot": "TinyHardpoint4", "BuyItem": item})
    assert plugin.get_session()["completed_steps"] == ["1", "2"]


def test_automatic_completion_preserves_maximum_strict_engineering_matching(tmp_path):
    plugin = _plugin(tmp_path)
    item = "hpt_shieldbooster_size0_class5"
    steps = [
        {"id": "grade-3", "item": item, "engineering": {"BlueprintName": "ShieldBooster_Resistive", "Level": 3}},
        {"id": "grade-5", "item": item, "engineering": {"BlueprintName": "ShieldBooster_Resistive", "Level": 5}},
    ]
    modules = [
        {"item": item, "engineering": {"BlueprintName": "ShieldBooster_Resistive", "Level": grade, "Quality": 1}}
        for grade in (5, 3)
    ]
    assert plugin._satisfied_step_ids(steps, modules) == ["grade-3", "grade-5"]


def test_french_tool_names_distinguish_beam_and_pulse_lasers(tmp_path):
    plugin = _plugin(tmp_path)
    assert plugin._module_name_fr("hpt_beamlaser_gimbal_large") == "Laser à rayon"
    assert plugin._module_name_fr("hpt_pulselaser_gimbal_large") == "Laser à impulsions"


def test_engineer_craft_recommends_remaining_local_boosters_before_fsd(tmp_path: Path):
    plugin = _plugin(tmp_path)
    booster = "hpt_shieldbooster_size0_class5"
    plugin.import_plan("python", "Build", {"steps": [
        {"id": "booster-1", "slot": "TinyHardpoint1", "item": booster,
         "engineering": {"BlueprintName": "ShieldBooster_Resistive", "Level": 3}},
        {"id": "fsd", "slot": "FrameShiftDrive", "item": "int_hyperdrive_size4_class5",
         "engineering": {"BlueprintName": "FSD_LongRange", "Level": 5}},
        {"id": "booster-2", "slot": "TinyHardpoint2", "item": booster,
         "engineering": {"BlueprintName": "ShieldBooster_Resistive", "Level": 3}},
        {"id": "booster-3", "slot": "TinyHardpoint3", "item": booster,
         "engineering": {"BlueprintName": "ShieldBooster_Resistive", "Level": 3}},
    ]})
    plugin._handle_ship_event({
        "event": "Loadout", "Ship": "python", "ShipID": 1,
        "Modules": [
            {"Slot": "TinyHardpoint1", "Item": booster},
            {"Slot": "FrameShiftDrive", "Item": "int_hyperdrive_size4_class5"},
            {"Slot": "TinyHardpoint2", "Item": booster},
            {"Slot": "TinyHardpoint3", "Item": booster},
        ],
    })
    plugin.start_session("Build", "1")
    plugin._handle_ship_event({
        "event": "Docked", "StarSystem": "Leesti", "StationName": "Vatermann LLC",
    })

    plugin._handle_ship_event({
        "event": "EngineerCraft",
        "Slot": "TinyHardpoint1",
        "Module": booster,
        "BlueprintName": "ShieldBooster_Resistive",
        "Level": 3,
        "Quality": 1,
    })

    assert plugin.get_session()["completed_steps"] == ["booster-1"]
    assert len(plugin.helper.events) == 1
    notice = plugin.helper.events[0].plugin_event_content
    assert "Next: Shield Booster" in notice
    assert "Frame Shift Drive" not in notice


def test_craft_keeps_already_installed_effect_and_completes_grade(tmp_path: Path):
    plugin = _plugin(tmp_path)
    plugin.import_plan("python", "Build", {"steps": [
        {"id": "fsd", "slot": "FrameShiftDrive", "item": "int_hyperdrive_size4_class5",
         "engineering": {"BlueprintName": "FSD_LongRange", "Level": 5,
                         "ExperimentalEffect": "special_fsd_fuelcapacity"}},
    ]})
    plugin.start_session("Build", "1")
    plugin._handle_ship_event({
        "event": "Loadout", "Ship": "python", "ShipID": 1,
        "Modules": [{"Slot": "FrameShiftDrive", "Item": "int_hyperdrive_size4_class5",
                     "Engineering": {"BlueprintName": "FSD_LongRange", "Level": 4,
                                     "ExperimentalEffect": "special_fsd_fuelcapacity"}}],
    })
    plugin._handle_ship_event({
        "event": "EngineerCraft", "Slot": "FrameShiftDrive", "Module": "int_hyperdrive_size4_class5",
        "BlueprintName": "FSD_LongRange", "Level": 5, "Quality": 1,
    })
    assert plugin.get_session()["completed_steps"] == ["fsd"]
    assert len(plugin.helper.events) == 1


def test_experimental_effect_can_be_only_work_remaining(tmp_path: Path):
    plugin = _plugin(tmp_path)
    plugin.import_plan("python", "Build", {"steps": [
        {"id": "fsd", "slot": "FrameShiftDrive", "item": "int_hyperdrive_size4_class5",
         "engineering": {"BlueprintName": "FSD_LongRange", "Level": 5,
                         "ExperimentalEffect": "special_fsd_fuelcapacity"}},
    ]})
    plugin.start_session("Build", "1")
    plugin._handle_ship_event({
        "event": "Loadout", "Ship": "python", "ShipID": 1,
        "Modules": [{"Slot": "FrameShiftDrive", "Item": "int_hyperdrive_size4_class5",
                     "Engineering": {"BlueprintName": "FSD_LongRange", "Level": 5, "Quality": 1}}],
    })
    assert plugin.get_session()["completed_steps"] == []
    assert "apply the experimental effect Deep Charge" in plugin._next_step_action()
    assert "complete the" not in plugin._next_step_action()
    assert "experimental effect Deep Charge to apply" in plugin._build_advice_action(
        BuildAdviceParams(detailed=True)
    )


def test_workshop_prioritizes_local_shields_and_explains_limit(tmp_path: Path):
    plugin = _plugin(tmp_path)
    plugin.import_plan("python", "Build", {"steps": [
        {"id": "fsd", "slot": "FrameShiftDrive", "item": "int_hyperdrive_size4_class5",
         "engineering": {"BlueprintName": "FSD_LongRange", "Level": 5}},
        {"id": "shield", "slot": "Slot01_Size5", "item": "int_shieldgenerator_size5_class5",
         "engineering": {"BlueprintName": "ShieldGenerator_Reinforced", "Level": 5}},
    ]})
    plugin.start_session("Build", "1")
    plugin._handle_ship_event({"event": "Loadout", "Ship": "python", "ShipID": 1,
                               "Modules": [{"Slot": "FrameShiftDrive", "Item": "int_hyperdrive_size4_class5"},
                                           {"Slot": "Slot01_Size5", "Item": "int_shieldgenerator_size5_class5"}]})
    plugin._handle_ship_event({"event": "Docked", "StarSystem": "Leesti", "StationName": "Vatermann LLC"})
    assert "Shield Generator" in plugin._next_step_action()
    assert "maximum grade 3" in plugin._next_step_action()
    assert "At Didi Vatermann" in plugin._next_step_action()
    assert "Frame Shift Drive" in plugin._next_step_action()
    assert "cannot be upgraded at this workshop" in plugin._next_step_action()


def test_workshop_question_uses_current_location_projection_after_restart(tmp_path: Path):
    plugin = _plugin(tmp_path)
    plugin.import_plan("python", "Build", {"steps": [
        {"id": "fsd", "slot": "FrameShiftDrive", "item": "int_hyperdrive_size4_class5",
         "engineering": {"BlueprintName": "FSD_LongRange", "Level": 5}},
        {"id": "shield", "slot": "Slot01_Size5", "item": "int_shieldgenerator_size5_class5",
         "engineering": {"BlueprintName": "ShieldGenerator_Reinforced", "Level": 5}},
        {"id": "booster", "slot": "TinyHardpoint1", "item": "hpt_shieldbooster_size0_class5",
         "engineering": {"BlueprintName": "ShieldBooster_HeavyDuty", "Level": 5,
                         "ExperimentalEffect": "special_shieldbooster_shieldstrength"}},
    ]})
    plugin.start_session("Build", "1")
    plugin._handle_ship_event({
        "event": "Loadout", "Ship": "python", "ShipID": 1, "Modules": [
            {"Slot": "FrameShiftDrive", "Item": "int_hyperdrive_size4_class5"},
            {"Slot": "Slot01_Size5", "Item": "int_shieldgenerator_size5_class5"},
            {"Slot": "TinyHardpoint1", "Item": "hpt_shieldbooster_size0_class5"},
        ],
    })

    class EventManager:
        def get_current_state(self):
            return [], {"Location": {
                "StarSystem": "Leesti", "Station": "Vatermann LLC", "Docked": True,
            }, "EngineerProgress": {"Engineers": [
                {"Engineer": "Didi Vatermann", "Progress": "Unlocked", "Rank": 4},
            ]}}

    plugin.helper._event_manager = EventManager()
    assert plugin._current_workshop is None
    answer = plugin._workshop_upgrades_action()
    assert "Didi Vatermann" in answer
    assert "planned upgrades for Build" in answer
    assert "Shield Generator" in answer
    assert "Shield Booster" in answer
    assert "grade 3" in answer
    assert "grade 4" in answer
    assert "experimental effect" in answer
    planned, alternatives = answer.split("Other options at this workshop, NOT in the active plan")
    assert "Frame Shift Drive" not in planned
    assert "Frame Shift Drive" not in alternatives
    assert "Thermal resistant (up to grade 3)" in alternatives
    assert "Heavy duty" not in alternatives
    assert "Shield Generator" in plugin._next_step_action()
    status = plugin._status_generator({
        "Location": {
            "StarSystem": "Leesti", "Station": "Vatermann LLC", "Docked": True,
        }
    })[0][1]
    assert "at Didi Vatermann, planned upgrades supported here" in status
    assert "Reinforced up to grade 3" in status
    assert "Heavy duty up to grade 4" in status
    workshop_status, overview = status.split("Remaining upgrades overview: ")
    assert "Frame Shift Drive" not in workshop_status
    assert json.loads(overview)["remaining_module_count"] == 3


def test_workshop_recommends_exact_plan_before_nonplan_options(tmp_path: Path):
    plugin = _plugin(tmp_path)
    plugin.import_plan("python", "Build", {"steps": [
        {"id": "booster", "slot": "TinyHardpoint1", "item": "hpt_shieldbooster_size0_class5",
         "engineering": {"BlueprintName": "ShieldBooster_Resistive", "Level": 5}},
        {"id": "shield", "slot": "Slot01_Size5", "item": "int_shieldgenerator_size5_class5",
         "engineering": {"BlueprintName": "ShieldGenerator_Thermic", "Level": 3}},
    ]})
    plugin.start_session("Build", "1")
    plugin._handle_ship_event({
        "event": "Loadout", "Ship": "python", "ShipID": 1,
        "Modules": [
            {"Slot": "TinyHardpoint1", "Item": "hpt_shieldbooster_size0_class5"},
            {"Slot": "Slot01_Size5", "Item": "int_shieldgenerator_size5_class5"},
        ],
    })
    plugin._handle_ship_event({
        "event": "Docked", "StarSystem": "Leesti", "StationName": "Vatermann LLC",
    })

    planned, alternatives = plugin._workshop_upgrades_action().split(
        "Other options at this workshop, NOT in the active plan"
    )
    assert "Shield Booster A with Resistance augmented up to grade 5" in planned
    assert "Shield Generator 5A with Thermal resistant up to grade 3" in planned
    assert "Heavy duty" not in planned
    assert "Reinforced" not in planned
    assert "Heavy duty (up to grade 5)" in alternatives
    assert "Reinforced (up to grade 3)" in alternatives
    assert "Shield Booster A" in plugin._next_step_action()


def test_workshop_without_planned_work_separates_local_options(tmp_path: Path):
    plugin = _plugin(tmp_path)
    plugin.import_plan("python", "Build", {"steps": [
        {"id": "fsd", "slot": "FrameShiftDrive", "item": "int_hyperdrive_size4_class5",
         "engineering": {"BlueprintName": "FSD_LongRange", "Level": 5}},
        {"id": "shield", "slot": "Slot01_Size5", "item": "int_shieldgenerator_size5_class5",
         "engineering": {"BlueprintName": "ShieldGenerator_Thermic", "Level": 5}},
    ]})
    plugin.start_session("Build", "1")
    plugin._handle_ship_event({
        "event": "Loadout", "Ship": "python", "ShipID": 1,
        "Modules": [
            {"Slot": "FrameShiftDrive", "Item": "int_hyperdrive_size4_class5"},
            {"Slot": "Slot01_Size5", "Item": "int_shieldgenerator_size5_class5",
             "Engineering": {"BlueprintName": "ShieldGenerator_Thermic", "Level": 3,
                             "Quality": 1}},
        ],
    })
    plugin._handle_ship_event({
        "event": "Docked", "StarSystem": "Leesti", "StationName": "Vatermann LLC",
    })
    answer = plugin._workshop_upgrades_action()
    assert "no remaining planned upgrades can be performed" in answer
    assert "Other options at this workshop, NOT in the active plan" in answer
    assert "Reinforced (up to grade 3)" in answer
    assert "Frame Shift Drive" not in answer
    assert "no remaining planned upgrades can be performed" in plugin._next_step_action()
    assert "grade availability is conditional" in answer


def test_workshop_alternatives_respect_rank_and_installed_modules(tmp_path: Path):
    plugin = _plugin(tmp_path)
    plugin.import_plan("python", "Build", {"steps": [
        {"id": "booster", "slot": "TinyHardpoint1", "item": "hpt_shieldbooster_size0_class5",
         "engineering": {"BlueprintName": "ShieldBooster_Resistive", "Level": 5}},
        {"id": "missing", "slot": "Slot01_Size5", "item": "int_shieldgenerator_size5_class5",
         "engineering": {"BlueprintName": "ShieldGenerator_Thermic", "Level": 3}},
    ]})
    plugin.start_session("Build", "1")
    plugin._handle_ship_event({
        "event": "Loadout", "Ship": "python", "ShipID": 1,
        "Modules": [{"Slot": "TinyHardpoint1", "Item": "hpt_shieldbooster_size0_class5"}],
    })
    plugin._handle_ship_event({
        "event": "Docked", "StarSystem": "Leesti", "StationName": "Vatermann LLC",
    })

    class EventManager:
        def get_current_state(self):
            return [], {"EngineerProgress": {"Engineers": [
                {"Engineer": "Didi Vatermann", "Progress": "Unlocked", "Rank": 2},
            ]}}

    plugin.helper._event_manager = EventManager()
    answer = plugin._workshop_upgrades_action()
    planned, alternatives = answer.split("Other options at this workshop, NOT in the active plan")
    assert "Resistance augmented up to grade 2" in planned
    assert "your unlocked rank is 2" in planned
    assert "Heavy duty (up to grade 2)" in alternatives
    assert "Shield Generator" not in alternatives
    assert "grade availability is conditional" not in answer


def test_workshop_question_does_not_fall_back_to_fsd_if_location_unknown(tmp_path: Path):
    plugin = _plugin(tmp_path)
    plugin.import_plan("python", "Build", {"steps": [
        {"id": "fsd", "item": "int_hyperdrive_size4_class5",
         "engineering": {"BlueprintName": "FSD_LongRange", "Level": 5}},
    ]})
    plugin.start_session("Build", "1")
    answer = plugin._workshop_upgrades_action()
    assert "cannot identify" in answer.lower()
    assert "FSD" not in answer


def test_next_step_at_confirmed_workshop_does_not_suggest_unsupported_fsd(tmp_path: Path):
    plugin = _plugin(tmp_path)
    plugin.import_plan("python", "Build", {"steps": [
        {"id": "fsd", "slot": "FrameShiftDrive", "item": "int_hyperdrive_size4_class5",
         "engineering": {"BlueprintName": "FSD_LongRange", "Level": 5}},
    ]})
    plugin.start_session("Build", "1")
    plugin._handle_ship_event({
        "event": "Loadout", "Ship": "python", "ShipID": 1,
        "Modules": [{"Slot": "FrameShiftDrive", "Item": "int_hyperdrive_size4_class5"}],
    })
    plugin._handle_ship_event({
        "event": "Docked", "StarSystem": "Leesti", "StationName": "Vatermann LLC",
    })
    answer = plugin._next_step_action()
    assert "no remaining planned upgrades can be performed" in answer
    assert "Frame Shift Drive" not in answer


def test_jump_range_change_only_proposes_recalculation(tmp_path: Path):
    plugin = _plugin(tmp_path)
    plugin.import_plan("python", "Build", {"steps": [
        {"id": "fsd", "slot": "FrameShiftDrive", "item": "int_hyperdrive_size4_class5",
         "engineering": {"BlueprintName": "FSD_LongRange", "Level": 5}},
    ]})
    plugin.start_session("Build", "1")

    def report(ship_id, value):
        plugin._handle_ship_event({"event": "Loadout", "Ship": "python", "ShipID": ship_id,
                                   "MaxJumpRange": value, "Modules": []})

    report(1, 30.0)
    report(1, 30.05)
    report(2, 70.0)
    assert plugin.helper.events == []
    report(1, 30.2)
    assert len(plugin.helper.events) == 1
    assert "recalculate" in plugin.helper.events[0].plugin_event_content
    assert plugin._maximum_jump_range_by_ship["1"] == 30.2


def test_small_range_changes_accumulate_and_detailed_route_estimates_jumps(tmp_path: Path):
    from plugins.ship_upgrade_manager.planner import PlannedAction

    plugin = _plugin(tmp_path)
    plugin.import_plan("python", "Build", {"steps": [
        {"id": "fsd", "item": "int_hyperdrive_size4_class5",
         "engineering": {"BlueprintName": "FSD_LongRange", "Level": 5}},
    ]})
    plugin.start_session("Build", "1")
    for value in (20.0, 20.05, 20.11):
        plugin._handle_ship_event({
            "event": "Loadout", "Ship": "python", "ShipID": 1,
            "MaxJumpRange": value, "Modules": [],
        })
    assert len(plugin.helper.events) == 1
    assert "20.0 to 20.1" in plugin.helper.events[0].plugin_event_content
    plugin._handle_ship_event({
        "event": "Loadout", "Ship": "python", "ShipID": 1,
        "MaxJumpRange": 20.01, "Modules": [],
    })
    assert len(plugin.helper.events) == 2
    leg = PlannedAction("engineer_visit", "fsd", "Felicity Farseer",
                        system="Deciat", station="Farseer Inc", leg_distance_ly=41)
    assert "at least 3 estimated jumps" in plugin._detailed_route([leg], "1", [])
    assert "jumps unknown" in plugin._detailed_route([leg], "2", [])


def test_workshop_respects_player_rank_even_when_registry_supports_higher_grade(tmp_path: Path):
    plugin = _plugin(tmp_path)
    plugin.import_plan("python", "Build", {"steps": [
        {"id": "shield", "slot": "Slot01_Size5", "item": "int_shieldgenerator_size5_class5",
         "engineering": {"BlueprintName": "ShieldGenerator_Reinforced", "Level": 5}},
    ]})
    plugin.start_session("Build", "1")
    plugin._handle_ship_event({
        "event": "Loadout", "Ship": "python", "ShipID": 1,
        "Modules": [{"Slot": "Slot01_Size5", "Item": "int_shieldgenerator_size5_class5"}],
    })
    plugin._handle_ship_event({"event": "Docked", "StarSystem": "Leesti", "StationName": "Vatermann LLC"})

    class EventManager:
        def get_current_state(self):
            return [], {"EngineerProgress": {"Engineers": [
                {"Engineer": "Didi Vatermann", "Progress": "Unlocked", "Rank": 2},
            ]}}

    plugin.helper._event_manager = EventManager()
    answer = plugin._next_step_action()
    assert "up to grade 2" in answer
    assert "maximum grade 3; your unlocked rank is 2" in answer


def test_route_default_summary_and_detailed_jump_estimate(tmp_path: Path):
    plugin = _plugin(tmp_path)
    plugin.import_plan("python", "Build", {"steps": [
        {"id": "fsd", "slot": "FrameShiftDrive", "item": "int_hyperdrive_size4_class5",
         "engineering": {"BlueprintName": "FSD_LongRange", "Level": 5,
                         "ExperimentalEffect": "special_fsd_fuelcapacity"}},
    ]})
    plugin.start_session("Build", "1")
    plugin._handle_ship_event({"event": "Loadout", "Ship": "python", "ShipID": 1,
                               "MaxJumpRange": 20.0, "Modules": [
                                   {"Slot": "FrameShiftDrive", "Item": "int_hyperdrive_size4_class5"}
                               ]})
    plugin._current_position = (0, 0, 0)
    summary = plugin._build_advice_action()
    detailed = plugin._build_advice_action(BuildAdviceParams(detailed=True))

    assert "Route ready to upgrade 1 remaining module" in summary
    assert "Ask for the detailed route" in summary
    assert "estimated jumps" in detailed
    assert "Deep Charge to apply" in detailed


def test_calculated_route_is_published_and_follows_current_ship(tmp_path: Path, monkeypatch):
    plugin = _plugin(tmp_path)
    plugin.import_plan("python", "Route build", {"steps": [
        {"id": "fsd", "slot": "FrameShiftDrive", "item": "int_hyperdrive_size4_class5",
         "engineering": {"BlueprintName": "FSD_LongRange", "Level": 5,
                         "ExperimentalEffect": "special_fsd_fuelcapacity"}},
    ]})
    plugin.start_session("Route build", "1")
    plugin._handle_ship_event({"event": "Loadout", "Ship": "python", "ShipID": 1,
                               "MaxJumpRange": 20.0, "Modules": [
                                   {"Slot": "FrameShiftDrive", "Item": "int_hyperdrive_size4_class5"}
                               ]})
    plugin._current_position = (0, 0, 0)
    publications = []
    monkeypatch.setattr(plugin, "_republish", lambda: publications.append(True))
    assert plugin._field("route", "route_empty")["content"] == "plugin.sum.route.empty"

    plugin._build_advice_action()

    summary = plugin._field("route", "route_summary")
    assert summary["params"] == {"plan": "Route build", "ship": "Python", "modules": 1}
    assert summary["label"] is None
    fields = next(grid["fields"] for grid in plugin.settings_config["grids"] if grid["key"] == "route")
    visits = [field for field in fields if field.get("content") == "plugin.sum.route.engineer_visit"]
    assert visits and visits[0]["params"]["stop"] == 1
    assert visits[0]["params"]["system"] != "—"
    assert any(field.get("content") == "plugin.sum.route.jumps" for field in fields)
    assert any(field.get("params", {}).get("target") == "Deep Charge" for field in fields)
    assert publications

    plugin._handle_ship_event({"event": "Loadout", "Ship": "anaconda", "ShipID": 2, "Modules": []})
    assert plugin._field("route", "route_empty")["content"] == "plugin.sum.route.empty"
    plugin._handle_ship_event({"event": "Loadout", "Ship": "python", "ShipID": 1, "Modules": []})
    assert plugin._field("route", "route_summary")["params"]["plan"] == "Route build"
    plugin._current_fid = "another-commander"
    plugin._publish_status()
    assert plugin._field("route", "route_empty")["content"] == "plugin.sum.route.empty"


def test_route_snapshot_keeps_unknown_distances_and_partial_scope(tmp_path: Path):
    from plugins.ship_upgrade_manager.planner import PlannedAction

    plugin = _plugin(tmp_path)
    plugin._current_ship_id = "1"
    plugin._publish_route([
        PlannedAction("engineer_visit", None, "Engineer", system="Sol"),
        PlannedAction("apply_blueprint", "FrameShiftDrive", "FSD_LongRange", grade=5,
                      stock_status="missing", cost_status="unknown"),
    ], [], {"ship_instance_id": "1", "ship_name": "My ship", "plan_name": "Build"}, "FSD")

    assert plugin._field("route", "route_scope")["params"]["module"] == "FSD"
    assert plugin._field("route", "route_leg_0")["content"] == "plugin.sum.route.unknownOriginDistance"
    assert plugin._field("route", "route_jumps_0")["content"] == "plugin.sum.route.unknownOriginJumps"
    assert plugin._field("route", "route_stock_1")["content"] == "plugin.sum.route.materialLimitNone"
    assert plugin._field("route", "route_cost_1")["content"] == "plugin.sum.route.unknownMaterials"


def test_route_stays_display_only_and_clears_empty_message_after_calculation(tmp_path: Path):
    plugin = _plugin(tmp_path)
    plugin.import_plan("python", "Button route", {"steps": [
        {"id": "fsd", "slot": "FrameShiftDrive", "item": "int_hyperdrive_size4_class5",
         "engineering": {"BlueprintName": "FSD_LongRange", "Level": 5}},
    ]})
    plugin.start_session("Button route", "1")
    plugin._handle_ship_event({"event": "Loadout", "Ship": "python", "ShipID": 1, "Modules": [
        {"Slot": "FrameShiftDrive", "Item": "int_hyperdrive_size4_class5"},
    ]})
    assert all(field["type"] != "button" for field in plugin._grid("route")["fields"])
    assert plugin._field("route", "route_empty")["content"] == "plugin.sum.route.empty"

    plugin._build_advice_action()

    assert plugin._field("route", "route_summary")["params"]["plan"] == "Button route"
    assert all(field["type"] != "button" for field in plugin._grid("route")["fields"])
    assert "plugin.sum.route.empty" not in _route_kinds(plugin)


@pytest.mark.parametrize("running", [False, True])
def test_removed_route_button_reports_an_invalid_action(tmp_path: Path, monkeypatch, running: bool):
    plugin = _plugin(tmp_path, running=running)
    monkeypatch.setattr(plugin, "_build_advice_action", lambda: pytest.fail("Unexpected calculation"))
    emitted = []
    monkeypatch.setattr("lib.PluginManager.emit_message",
                        lambda kind, **payload: emitted.append((kind, payload)))
    plugin._manager.on_ui_action(
        plugin.plugin_manifest.guid, "settings_button", {"key": "calculate_route"}, "obsolete",
    )
    kind, result = emitted[-1]
    assert kind == "plugin_ui_result"
    assert result["success"] is False
    assert result["error"] == "common.invalidAction"


def test_route_groups_grades_per_module_and_per_engineer(tmp_path: Path):
    from plugins.ship_upgrade_manager.planner import PlannedAction, analyze_build

    plugin = _plugin(tmp_path)
    plugin._current_ship_id = "1"
    steps = [{"slot": "PowerDistributor", "item": "int_powerdistributor_size7_class5",
              "engineering": {"BlueprintName": "PowerDistributor_HighFrequency", "Level": 5}}]
    deltas = analyze_build(steps, [])
    actions = [PlannedAction("engineer_visit", None, "First", system="Sol")]
    actions += [PlannedAction("apply_blueprint", "PowerDistributor", "PowerDistributor_HighFrequency",
                              grade=grade, stock_status="missing" if grade == 1 else "sufficient")
                for grade in range(1, 4)]
    actions += [PlannedAction("engineer_visit", None, "The Dweller", system="Wyrd", station="Black Hide")]
    actions += [PlannedAction("apply_blueprint", "PowerDistributor", "PowerDistributor_HighFrequency", grade=grade)
                for grade in range(4, 6)]
    actions += [PlannedAction("experimental", "PowerDistributor", "Super Conduits")]

    plugin._publish_route(actions, deltas, {"ship_instance_id": "1", "ship_name": "Ship", "plan_name": "Build"}, None)

    fields = next(grid["fields"] for grid in plugin.settings_config["grids"] if grid["key"] == "route")
    blueprints = [field for field in fields if field["key"].startswith("route_action_") and field.get("params", {}).get("grade")]
    assert len(blueprints) == 2
    assert blueprints[0]["params"]["grade"] == 3
    assert blueprints[0]["params"]["targetGrade"] == 5
    assert blueprints[0]["content"] == "plugin.sum.route.applyBlueprintGoal"
    assert blueprints[1]["params"]["grade"] == 5
    assert blueprints[1]["params"]["module"] == "Power Distributor"
    assert blueprints[1]["params"]["slot"] == "PowerDistributor"
    assert plugin._field("route", "route_stock_1")["content"] == "plugin.sum.route.materialLimitNone"


def test_voice_route_publishes_latest_config_using_projected_ship(tmp_path: Path, monkeypatch):
    from copy import deepcopy
    from lib.PluginManager import PluginManager

    plugin = _plugin(tmp_path)
    plugin.import_plan("python", "Voice route", {"steps": [
        {"id": "fsd", "slot": "FrameShiftDrive", "item": "int_hyperdrive_size4_class5",
         "engineering": {"BlueprintName": "FSD_LongRange", "Level": 5}},
    ]})
    plugin.start_session("Voice route", "1")
    plugin._current_ship_id = "2"
    plugin._current_position = (0, 0, 0)

    class EventManager:
        def get_current_state(self):
            return [], {"Loadout": {"Ship": "python", "ShipID": 1, "MaxJumpRange": 20.0,
                                   "Modules": [{"Slot": "FrameShiftDrive", "Item": "int_hyperdrive_size4_class5"}]}}

    plugin.helper._event_manager = EventManager()
    manager = PluginManager({"plugin_settings": {}})
    manager.plugin_list[plugin.plugin_manifest.guid] = plugin
    manager.register_settings()
    manager.plugin_settings_configs[plugin.plugin_manifest.guid] = deepcopy(plugin.settings_config)
    emitted = []
    monkeypatch.setattr("lib.PluginManager.emit_message", lambda event, **payload: emitted.append((event, deepcopy(payload))))

    plugin._build_advice_action(BuildAdviceParams(), {})

    assert plugin._current_ship_id == "1"
    published = next(payload for kind, payload in reversed(emitted) if kind == "plugin_settings_configs")[
        "plugin_settings_configs"
    ][plugin.plugin_manifest.guid]
    route = next(grid for grid in published["grids"] if grid["key"] == "route")
    assert all(field["type"] != "button" for field in route["fields"])
    content = json.dumps(route["fields"])
    assert 'plugin.test-guid.route.empty' not in content
    assert 'plugin.test-guid.route.jumps' in content


def test_jump_range_available_from_existing_projection_and_latest_report(tmp_path: Path):
    from pydantic import BaseModel

    class LoadoutState(BaseModel):
        ShipID: int = 1
        MaxJumpRange: float | None = None

    class EventManager:
        def get_current_state(self):
            return [], {"Loadout": loadout, "ShipInfo": {"ReportedMaximumJumpRange": 25.0}}

    loadout = LoadoutState()
    plugin = _plugin(tmp_path)
    plugin.helper._event_manager = EventManager()
    assert plugin._maximum_jump_range("1") == 25.0
    loadout.MaxJumpRange = 30.0
    assert plugin._maximum_jump_range("1") == 30.0
    assert plugin._maximum_jump_range("2") is None


def test_jump_range_available_from_loadout_written_before_plugin_start(tmp_path: Path, monkeypatch):
    journal = tmp_path / "Journal.test.log"
    journal.write_text(json.dumps({"event": "Loadout", "ShipID": 1, "MaxJumpRange": 22.5, "Modules": []}), encoding="utf-8")
    plugin = _plugin(tmp_path)
    monkeypatch.setattr(plugin, "journal_files", lambda: [str(journal)])

    assert plugin._maximum_jump_range("1") == 22.5
    assert plugin._maximum_jump_range("2") is None


def test_route_weapon_limitations_have_display_names_and_grouped_grades(tmp_path: Path):
    from plugins.ship_upgrade_manager.planner import analyze_build, build_advice

    plugin = _plugin(tmp_path)
    plugin._current_ship_id = "1"
    steps = [{"slot": f"MediumHardpoint{index}", "item": "hpt_railgun_fixed_medium",
              "engineering": {"BlueprintName": "Weapon_LongRange", "Level": 5}} for index in (1, 2)]
    deltas = analyze_build(steps, [{"Slot": step["slot"], "Item": step["item"]} for step in steps])
    actions = build_advice(deltas, plugin._catalog, {})

    plugin._publish_route(actions, deltas, {"ship_instance_id": "1", "ship_name": "Ship", "plan_name": "Build"}, None)

    fields = plugin._grid("route")["fields"]
    limitations = [field for field in fields if field.get("content") == "plugin.sum.route.noEngineerData"]
    assert len(limitations) == 2
    assert all(field["params"]["target"] == "Long range" for field in limitations)
    assert all(field["params"]["module"] == "Railgun" for field in limitations)
    assert all(field["params"]["grades"] == "1–5" for field in limitations)
    summary = plugin._route_summary(actions, deltas)
    assert summary.count("no engineer data") == 1


@pytest.mark.parametrize("ship_name,expected", [("", "Kestrel Mk II"), ("My Kestrel", "My Kestrel")])
def test_route_summary_uses_ship_name_or_model_not_internal_id(tmp_path: Path, ship_name, expected):
    plugin = _plugin(tmp_path)
    plugin._current_ship_id = "15"
    plugin._current_ship_type = "smallcombat01_nx"
    plugin._publish_route([], [], {"ship_instance_id": "15", "ship_name": ship_name, "plan_name": "Imported Kestrel Mk II"}, None)

    assert plugin._field("route", "route_summary")["params"]["ship"] == expected


def test_route_calculation_recovers_origin_from_location_projection(tmp_path: Path):
    from lib.projections.location import LocationState

    plugin = _plugin(tmp_path)
    plugin.import_plan("smallcombat01_nx", "Imported Kestrel Mk II", {"steps": [
        {"id": "rail", "slot": "MediumHardpoint1", "item": "hpt_railgun_fixed_medium",
         "engineering": {"BlueprintName": "Weapon_LongRange", "Level": 5}},
    ]})
    plugin.start_session("Imported Kestrel Mk II", "15")

    class EventManager:
        def get_current_state(self):
            return [], {
                "Loadout": {"ShipID": 15, "Ship": "smallcombat01_nx", "MaxJumpRange": 27.0,
                            "Modules": [{"Slot": "MediumHardpoint1", "Item": "hpt_railgun_fixed_medium"}]},
                "Location": LocationState(StarSystem="Muth", StarPos=[41, -30.59375, -19], Station="Gutierrez Hub", Docked=True),
                "Engineers": {"Engineers": [{"Engineer": "Tod 'The Blaster' McQuinn", "Progress": "Unlocked", "Rank": 5}]},
            }

    plugin.helper._event_manager = EventManager()
    plugin._build_advice_action()

    assert plugin._current_position == (41, -30.59375, -19)
    assert plugin._field("route", "route_summary")["params"]["ship"] == "Kestrel Mk II"
    assert plugin._field("route", "route_origin")["params"]["location"] == "Muth / Gutierrez Hub"
    leg = plugin._field("route", "route_leg_0")
    assert leg["content"] == "plugin.sum.route.firstLeg"
    assert leg["params"]["distance"] == 110.2
    assert plugin._field("route", "route_jumps_0")["params"]["jumps"] == 5


@pytest.mark.parametrize("undocked", [False, True])
def test_route_origin_recovered_from_latest_journal_location(tmp_path: Path, monkeypatch, undocked):
    journal = tmp_path / "Journal.test.log"
    entries = [
        {"event": "Location", "StarSystem": "Sol", "StarPos": [0, 0, 0], "Docked": True, "StationName": "Old station"},
        {"event": "FSDJump", "StarSystem": "Muth", "StarPos": [41, -30.59375, -19]},
        {"event": "Docked", "StarSystem": "Muth", "StationName": "Gutierrez Hub"},
    ]
    if undocked:
        entries.append({"event": "Undocked", "StationName": "Gutierrez Hub"})
    journal.write_text("\n".join(json.dumps(entry) for entry in entries), encoding="utf-8")
    plugin = _plugin(tmp_path)
    monkeypatch.setattr(plugin, "journal_files", lambda: [str(journal)])

    plugin._sync_route_location({})

    assert plugin._current_position == (41, -30.59375, -19)
    assert plugin._current_system == "Muth"
    assert plugin._current_docked is not undocked
    if not undocked:
        assert plugin._current_station == "Gutierrez Hub"


@pytest.mark.parametrize("docked,leg,expected_leg,expected_jumps", [
    (True, 0.0, "alreadyHere", "noJumps"),
    (False, 0.0, "sameSystem", "noJumps"),
    (False, None, "sameSystem", "noJumps"),
])
def test_route_distinguishes_same_system_and_missing_stop_coordinates(tmp_path: Path, docked, leg, expected_leg, expected_jumps):
    from plugins.ship_upgrade_manager.planner import PlannedAction

    plugin = _plugin(tmp_path)
    plugin._current_ship_id = "15"
    plugin._current_position = (40, 79.21875, -10.40625)
    plugin._current_system = "Wolf 397"
    plugin._current_station = "Trophy Camp"
    plugin._current_docked = docked
    plugin._publish_route([
        PlannedAction("engineer_visit", None, "Tod 'The Blaster' McQuinn", system="Wolf 397", station="Trophy Camp", leg_distance_ly=leg),
    ], [], {"ship_instance_id": "15", "ship_name": "", "plan_name": "Build"}, None)

    assert plugin._field("route", "route_leg_0")["content"] == f"plugin.sum.route.{expected_leg}"
    assert plugin._field("route", "route_jumps_0")["content"] == f"plugin.sum.route.{expected_jumps}"


def test_route_unknown_destination_coordinates_are_not_missing_origin(tmp_path: Path):
    from plugins.ship_upgrade_manager.planner import PlannedAction

    plugin = _plugin(tmp_path)
    plugin._current_ship_id = "15"
    plugin._current_position = (41, -30.59375, -19)
    plugin._current_system = "Muth"
    plugin._publish_route([
        PlannedAction("station_purchase", None, "Beam Laser", system="Unknown destination", station="Station"),
    ], [], {"ship_instance_id": "15", "ship_name": "", "plan_name": "Build"}, None)

    assert plugin._field("route", "route_leg_0")["content"] == "plugin.sum.route.unknownDistance"
    assert plugin._field("route", "route_jumps_0")["content"] == "plugin.sum.route.unknownJumpsDistance"


def test_route_unknown_location_projection_does_not_become_sol(tmp_path: Path):
    from lib.projections.location import LocationState

    plugin = _plugin(tmp_path)
    plugin._sync_route_location({"Location": LocationState()})

    assert plugin._current_position is None
    assert plugin._current_system == ""


def test_route_publishes_purchase_items_separately(tmp_path: Path):
    from plugins.ship_upgrade_manager.planner import PlannedAction

    plugin = _plugin(tmp_path)
    plugin._current_ship_id = "15"
    modules = ["Mk II Plasma Shock Accelerator", "Beam Laser", "Guardian Module Reinforcement"]
    plugin._publish_route([
        PlannedAction("station_purchase", None, ", ".join(modules), system="Wu Kuku", station="Al Saud Terminal", modules=modules),
    ], [], {"ship_instance_id": "15", "ship_name": "", "plan_name": "Build"}, None)

    fields = plugin._grid("route")["fields"]
    assert [field["params"]["module"] for field in fields if field["key"].startswith("route_purchase_")] == modules


def test_route_publishes_unlock_shortfalls_locked_purchase_and_partial_engineering(tmp_path: Path):
    from plugins.ship_upgrade_manager.planner import PlannedAction, analyze_build

    plugin = _plugin(tmp_path)
    plugin._current_ship_id = "15"
    plugin._current_fid = "F1"
    plugin._broker_set_status("guardian_module_reinforcement_package", "locked")
    steps = [
        {"slot": "Slot1", "item": "int_guardianmodulereinforcement_size2_class1"},
        {"slot": "Slot2", "item": "int_hullreinforcement_size2_class1", "engineering": {"BlueprintName": "HullReinforcement_HeavyDuty", "Level": 5}},
    ]
    deltas = analyze_build(steps, [])
    actions = [
        PlannedAction("unlock_required", "slot1", "Guardian Module Reinforcement", stock_status="missing",
                      missing_resources=[{"name": "Reinforced Mounting Plate", "name_fr": "Plaque de montage renforcée", "missing": 9, "quantity": 9, "type": "commodity"}]),
        PlannedAction("station_purchase", None, "Guardian Module Reinforcement", module_items=[{
            "name": "Guardian Module Reinforcement", "item": steps[0]["item"], "slot": "slot1"}]),
        PlannedAction("engineer_visit", None, "Selene Jean", system="Kuk", station="Prospector's Rest"),
        PlannedAction("apply_blueprint", "slot2", "HullReinforcement_HeavyDuty", grade=5, stock_status="missing",
                      reachable_grade=3, reachable_increment=2,
                      missing_resources=[{"name": "Iron", "missing": 2, "quantity": 6, "type": "material"}]),
    ]

    plugin._publish_route(actions, deltas, {"ship_instance_id": "15", "ship_name": "Ship", "plan_name": "Build"}, None)

    fields = plugin._grid("route")["fields"]
    assert len([field for field in fields if field.get("content") == "plugin.sum.route.needResource"]) == 2
    assert plugin._field("route", "route_purchase_1_0")["content"] == "plugin.sum.route.purchaseLocked"
    assert plugin._field("route", "route_action_3")["params"]["purchaseRequired"] == "yes"
    assert plugin._field("route", "route_stock_3")["params"] == {"grade": 3, "increment": 2}
    assert plugin._field("route", "route_action_2")["params"]["stop"] == 3


@pytest.mark.parametrize("ready", [True, False])
def test_requested_route_integrates_locked_broker_product_and_actual_inventory(tmp_path: Path, monkeypatch, ready):
    plugin = _plugin(tmp_path)
    plugin._current_fid = "F1"
    plugin._broker_set_status("guardian_module_reinforcement_package", "locked")
    item = "int_guardianmodulereinforcement_size2_class1"
    plugin.import_plan("python", "Guardian route", {"steps": [{"id": "mrp", "slot": "Slot1", "item": item}]})
    plugin.start_session("Guardian route", "15")
    product = next(product for product in plugin._broker_products if product["broker_id"] == "guardian_module_reinforcement_package")

    class EventManager:
        def get_current_state(self):
            return [], {
                "Loadout": {"ShipID": 15, "Ship": "python", "Modules": [], "MaxJumpRange": 20},
                "Location": {"StarSystem": "Sol", "StarPos": [0, 0, 0]},
                "Materials": {"LastUpdated": "today", "Manufactured": [
                    {"Name": requirement["covas_id"], "Count": requirement["quantity"]}
                    for requirement in product["requirements"] if requirement["type"] != "commodity"
                ]},
                "Cargo": {"timestamp": "today", "Vessel": "Ship", "Inventory": [
                    {"Name": "reinforcedmountingplate", "Count": 9 if ready else 2}
                ]},
            }

    plugin.helper._event_manager = EventManager()
    plugin.helper.search_stations_for_modules = lambda filters, **kwargs: {
        "status": "ok", "stations": [{"name": "Purchase", "system": "Far", "id": "purchase",
                                      "coords": {"x": 100, "y": 0, "z": 0}, "matched_filters": filters}],
    }
    searched = []
    monkeypatch.setattr(plugin, "_route_broker_search", lambda kind, states: searched.append(kind) or {
        "status": "ok", "stations": [{"name": "Broker", "system": "Path", "coords": {"x": 50, "y": 0, "z": 0}}],
    })

    result = plugin._build_advice_action()

    fields = plugin._grid("route")["fields"]
    assert any(field.get("content") == "plugin.sum.route.purchaseLocked" for field in fields)
    if ready:
        assert searched == ["Guardian"]
        kinds = [field.get("content") for field in fields if field["key"].startswith("route_action_")]
        assert kinds == ["plugin.sum.route.broker_visit", "plugin.sum.route.unlock_module", "plugin.sum.route.station_purchase"]
        assert not any(field.get("content") == "plugin.sum.route.needResource" for field in fields)
        assert "Technology Broker unlocks before purchase" in result
    else:
        assert searched == []
        [needed] = [field for field in fields if field.get("content") == "plugin.sum.route.needResource"]
        assert needed["params"]["missing"] == 7
        assert needed["params"]["resource"] == "Reinforced Mounting Plate"
        assert any(field.get("content") == "plugin.sum.route.unlock_required" for field in fields)


def _tracked_route(tmp_path, *, experimental=False):
    plugin = _plugin(tmp_path)
    plugin._current_fid = "F1"
    engineering = {"BlueprintName": "FSD_LongRange", "Level": 3}
    if experimental:
        engineering["ExperimentalEffect"] = "special_fsd_fuelcapacity"
    plan_id = plugin.import_plan("python", "Tracked route", {"steps": [
        {"id": "fsd", "slot": "FrameShiftDrive", "item": "int_hyperdrive_size4_class5", "engineering": engineering},
    ]})
    plugin.start_session("Tracked route", "1")
    plugin._handle_ship_event({"event": "Loadout", "ShipID": 1, "Ship": "python", "MaxJumpRange": 20,
                               "Modules": [{"Slot": "FrameShiftDrive", "Item": "int_hyperdrive_size4_class5"}]})
    plugin._current_position = (0, 0, 0)
    plugin._build_advice_action()
    return plugin, plan_id


def _craft_route(plugin, grade, quality=1, **extra):
    plugin._handle_ship_event({"event": "EngineerCraft", "Slot": "FrameShiftDrive",
                               "Module": "int_hyperdrive_size4_class5", "BlueprintName": "FSD_LongRange",
                               "Level": grade, "Quality": quality, **extra})


def _route_kinds(plugin):
    return [field.get("content") for field in plugin._grid("route")["fields"]]


def _navigation_result(system, *, status="ready"):
    return {
        "status": status, "system": system, "active_destination": system,
        "next_jump": "Intermediate", "message": "Navigation result",
    }


def _guardian_purchase_route(tmp_path, *, status="locked", mixed=True):
    from plugins.ship_upgrade_manager.planner import PlannedAction, analyze_build

    plugin = _plugin(tmp_path)
    plugin._handle_ship_event({"event": "LoadGame", "FID": "F1", "ShipID": 1, "Ship": "python"})
    plugin._broker_set_status("guardian_module_reinforcement_package", status)
    items = [{"slot": "Slot1", "item": "int_guardianmodulereinforcement_size2_class1",
              "name": "Guardian Module Reinforcement"}]
    if mixed:
        items.insert(0, {"slot": "LargeHardpoint1", "item": "hpt_beamlaser_gimbal_large", "name": "Beam Laser"})
    plugin.import_plan("python", "Purchases", {"steps": [
        {"id": item["slot"], "slot": item["slot"], "item": item["item"]} for item in items
    ]})
    plugin.start_session("Purchases", "1")
    plugin._handle_ship_event({"event": "Loadout", "ShipID": 1, "Ship": "python", "Modules": []})
    plugin._current_position = (0, 0, 0)
    target = plugin._upgrade_target()
    assert isinstance(target, dict)
    plugin._publish_route([
        PlannedAction("station_purchase", None, ", ".join(item["name"] for item in items),
                      system="Kachian", station="Goddard Station", coords=(10, 0, 0),
                      modules=[item["name"] for item in items], module_items=items),
    ], analyze_build(target["steps"], []), target, None)
    return plugin


@pytest.mark.parametrize("status", ["locked", "unknown"])
def test_next_purchase_reminder_excludes_restricted_guardian_and_always_warns(tmp_path, monkeypatch, status):
    plugin = _guardian_purchase_route(tmp_path, status=status)
    original = deepcopy(plugin._route_records[("F1", "1")])
    monkeypatch.setattr(plugin, "_build_advice_action", lambda *_: pytest.fail("Unexpected recalculation"))

    for _ in range(2):
        result = json.loads(plugin._next_stop_action(SessionActionParams(), {}))
        assert result["status"] == "next_stop"
        assert result["modules"] == ["Beam Laser"]
        assert result["purpose"] == "purchase Beam Laser"
        assert result["module_details"][0]["name_fr"] == "Laser à rayon"
        assert result["module_details"][0]["item"] == "hpt_beamlaser_gimbal_large"
        assert len(result["purchase_restrictions"]) == 1
        restriction = result["purchase_restrictions"][0]
        assert restriction["broker_id"] == "guardian_module_reinforcement_package"
        assert restriction["unlock_status"] == status
        assert "Technology Broker" in result["warnings"][0]
        assert ("NOT unlocked" if status == "locked" else "unknown") in result["warnings"][0]
        assert "ALWAYS relay" in result["response_policy"]
    record = plugin._route_records[("F1", "1")]
    assert record["calculated_at"] == original["calculated_at"]
    assert record["actions"] == original["actions"]


def test_saved_purchase_stop_drops_swapped_owned_weapons_without_recalculating(tmp_path, monkeypatch):
    from plugins.ship_upgrade_manager.planner import PlannedAction, analyze_build

    plugin = _plugin(tmp_path)
    plugin._handle_ship_event({"event": "LoadGame", "FID": "F1", "ShipID": 1, "Ship": "python"})
    plugin._broker_set_status("guardian_module_reinforcement_package", "locked")
    steps, modules = _swapped_weapons()
    guardian = {"id": "guardian", "slot": "Slot06_Size2",
                "item": "int_guardianmodulereinforcement_size2_class1"}
    steps.append(guardian)
    plugin.import_plan("python", "Swapped purchases", {"steps": steps})
    plugin.start_session("Swapped purchases", "1")
    plugin._handle_ship_event({"event": "Loadout", "ShipID": 1, "Ship": "python", "Modules": []})
    target = plugin._upgrade_target()
    assert isinstance(target, dict)
    purchase_steps = [steps[0], steps[2], guardian]
    items = [{"slot": step["slot"], "item": step["item"],
              "name": plugin._pretty_module_name(step["item"])} for step in purchase_steps]
    actions = [
        PlannedAction("station_purchase", None, ", ".join(item["name"] for item in items),
                      system="Kachian", station="Goddard Station", coords=(10, 0, 0),
                      modules=[item["name"] for item in items], module_items=items),
        PlannedAction("engineer_visit", None, "Broo Tarquin", system="Muang"),
        *[PlannedAction("apply_blueprint", "largehardpoint3", "Weapon_LongRange", grade=grade,
                        blueprint="Weapon_LongRange") for grade in range(1, 6)],
        PlannedAction("experimental", "largehardpoint3", "special_thermal_vent"),
    ]
    plugin._publish_route(actions, analyze_build(steps, []), target, None)
    before = deepcopy(plugin._route_records[("F1", "1")])
    monkeypatch.setattr(plugin, "_build_advice_action", lambda *_: pytest.fail("Unexpected recalculation"))
    plugin.helper.prepare_navigation = Mock()

    plugin._handle_ship_event({"event": "Loadout", "ShipID": 1, "Ship": "python", "Modules": modules})
    result = json.loads(plugin._prepare_next_jump_action(PrepareJumpParams(), {}))

    assert result["status"] == "blocked"
    assert result["modules"] == result["module_details"] == []
    assert result["weapon_slots"]["mismatch_count"] == 2
    assert "weapons" not in result["weapon_slots"]
    assert plugin._field("route", "route_weapon_slots")["content"] == "plugin.sum.route.weaponSlotMismatch"
    assert result["purchase_restrictions"][0]["broker_id"] == "guardian_module_reinforcement_package"
    plugin.helper.prepare_navigation.assert_not_called()
    record = plugin._route_records[("F1", "1")]
    assert record["actions"] == before["actions"]
    assert record["calculated_at"] == before["calculated_at"]
    remaining = record["remaining_actions"]
    assert remaining[0]["modules"] == ["Guardian Module Reinforcement"]
    assert [item["item"] for item in remaining[0]["module_items"]] == [guardian["item"]]
    assert [(action["grade"], action["increments"]) for action in remaining if action["kind"] == "apply_blueprint"] == [(5, 1)]
    assert any(action["kind"] == "experimental" and action["target"] == "special_thermal_vent" for action in remaining)
    plugin._handle_ship_event({"event": "ModuleSwap", "ShipID": 1,
                               "FromSlot": "LargeHardpoint1", "ToSlot": "LargeHardpoint3"})
    assert json.loads(plugin._weapon_slots_action(SessionActionParams(), {}))["mismatch_count"] == 0
    assert all(field["key"] != "route_weapon_slots" for field in plugin._grid("route")["fields"])


def test_misplaced_installed_weapons_do_not_block_engineer_navigation(tmp_path, monkeypatch):
    from plugins.ship_upgrade_manager.planner import PlannedAction, analyze_build

    plugin = _plugin(tmp_path)
    steps, modules = _swapped_weapons()
    plugin._handle_ship_event({"event": "LoadGame", "FID": "F1", "ShipID": 1, "Ship": "python"})
    plugin.import_plan("python", "Weapon engineering", {"steps": steps})
    plugin.start_session("Weapon engineering", "1")
    plugin._handle_ship_event({"event": "Loadout", "ShipID": 1, "Ship": "python", "Modules": modules})
    target = plugin._upgrade_target()
    assert isinstance(target, dict)
    plugin._publish_route([
        PlannedAction("engineer_visit", None, "Broo Tarquin", system="Muang", coords=(10, 0, 0)),
        PlannedAction("apply_blueprint", "largehardpoint3", "Weapon_LongRange",
                      grade=5, blueprint="Weapon_LongRange"),
        PlannedAction("experimental", "largehardpoint3", "special_thermal_vent"),
    ], analyze_build(steps, modules), target, None)
    monkeypatch.setattr(plugin, "_build_advice_action", lambda *_: pytest.fail("Unexpected recalculation"))
    plugin.helper.prepare_navigation = Mock(side_effect=_navigation_result)

    result = json.loads(plugin._prepare_next_jump_action(PrepareJumpParams(), {}))

    assert result["status"] == "ready"
    assert result["weapon_slots"]["mismatch_count"] == 2
    assert result["kind"] == "engineer_visit"
    assert "loadoutBlocked" not in result["flags"]
    plugin.helper.prepare_navigation.assert_called_once_with("Muang")


@pytest.mark.parametrize("status", ["locked", "unknown"])
def test_preparing_mixed_purchase_stop_preserves_the_unlock_warning(tmp_path, status):
    plugin = _guardian_purchase_route(tmp_path, status=status)
    plugin.helper.prepare_navigation = Mock(side_effect=_navigation_result)

    result = json.loads(plugin._prepare_next_jump_action(PrepareJumpParams(), {}))

    assert result["status"] == "ready"
    assert result["modules"] == ["Beam Laser"]
    assert result["purchase_restrictions"][0]["unlock_status"] == status
    assert result["warnings"]
    assert "ALWAYS relay" in result["response_policy"]
    plugin.helper.prepare_navigation.assert_called_once_with("Kachian")


@pytest.mark.parametrize("status", ["locked", "unknown"])
def test_guardian_only_purchase_stop_blocks_navigation_until_unlock_is_confirmed(tmp_path, status):
    plugin = _guardian_purchase_route(tmp_path, status=status, mixed=False)
    plugin.helper.prepare_navigation = Mock()

    result = json.loads(plugin._prepare_next_jump_action(PrepareJumpParams(), {}))

    assert result["status"] == "blocked"
    assert result["modules"] == []
    assert result["system"] == "Kachian"
    assert result["warnings"]
    assert result["purchase_restrictions"][0]["unlock_status"] == status
    plugin.helper.prepare_navigation.assert_not_called()


def test_guardian_purchase_becomes_available_only_after_unlock_event_without_recalculation(tmp_path, monkeypatch):
    plugin = _guardian_purchase_route(tmp_path, mixed=False)
    monkeypatch.setattr(plugin, "_build_advice_action", lambda *_: pytest.fail("Unexpected recalculation"))
    assert json.loads(plugin._next_stop_action(SessionActionParams(), {}))["status"] == "blocked"

    plugin._handle_ship_event({
        "event": "TechnologyBroker",
        "ItemsUnlocked": [{"Name": "int_guardianmodulereinforcement_size2_class1"}],
    })
    result = json.loads(plugin._next_stop_action(SessionActionParams(), {}))

    assert result["status"] == "next_stop"
    assert result["modules"] == ["Guardian Module Reinforcement"]
    assert result["purchase_restrictions"] == result["warnings"] == []
    assert plugin._field("route", "route_purchase_0_0")["content"] == "plugin.sum.route.purchaseModule"


@pytest.mark.parametrize("status", ["locked", "unknown", "unlocked"])
def test_broker_purchase_restrictions_are_consistent_in_summary_details_context_and_ui(tmp_path, status):
    from plugins.ship_upgrade_manager.planner import PlannedAction, SlotDelta

    plugin = _guardian_purchase_route(tmp_path, status=status)
    record = plugin._route_records[("F1", "1")]
    actions = [PlannedAction(**action) for action in record["actions"]]
    deltas = [SlotDelta(**delta) for delta in record["baseline"]]
    summary = plugin._route_summary(actions, deltas)
    detailed = plugin._detailed_route(actions, "1", deltas)
    context = plugin._route_prompt_status("1")
    fields = plugin._grid("route")["fields"]
    guardian = next(field for field in fields if field.get("params", {}).get("module") == "Guardian Module Reinforcement")
    if status == "unlocked":
        assert "WARNING:" not in summary + detailed
        assert guardian["content"] == "plugin.sum.route.purchaseModule"
        assert json.loads(context.split("Saved route status: ", 1)[1])["purchase_restrictions"] == []
    else:
        expected = "NOT unlocked and cannot be purchased yet" if status == "locked" else "unlock status is unknown"
        assert expected in summary and expected in detailed
        assert "purchase Beam Laser at Goddard Station" in summary
        assert "purchase Beam Laser, Guardian" not in summary
        assert f'"unlock_status": "{status}"' in context
        assert guardian["content"] == ("plugin.sum.route.purchaseLocked" if status == "locked"
                                       else "plugin.sum.route.purchaseUnknown")


def test_unlock_warning_is_applied_to_legacy_saved_route_with_only_module_names(tmp_path, monkeypatch):
    plugin = _guardian_purchase_route(tmp_path)
    record = plugin._route_records[("F1", "1")]
    for action in record["actions"]:
        action["module_items"] = []
    record.pop("remaining_actions")
    with plugin.get_db() as db:
        db.execute("UPDATE upgrade_routes SET snapshot = ?", (json.dumps(record),))
    restored = _plugin(tmp_path)
    restored._handle_ship_event({"event": "LoadGame", "FID": "F1", "ShipID": 1, "Ship": "python"})
    monkeypatch.setattr(restored, "_build_advice_action", lambda *_: pytest.fail("Unexpected recalculation"))

    result = json.loads(restored._next_stop_action(SessionActionParams(), {}))

    assert result["modules"] == ["Beam Laser"]
    assert result["purchase_restrictions"][0]["unlock_status"] == "locked"
    assert result["warnings"]


def test_ambiguous_legacy_broker_module_is_not_assumed_purchasable(tmp_path):
    from plugins.ship_upgrade_manager.planner import PlannedAction

    plugin = _plugin(tmp_path)
    plugin._broker_products = [
        {"broker_id": "small", "name_en": "Guardian Cannon Small", "name_fr": "Petit canon gardien"},
        {"broker_id": "large", "name_en": "Guardian Cannon Large", "name_fr": "Grand canon gardien"},
    ]

    result = plugin._purchase_advice(PlannedAction(
        "station_purchase", None, "Guardian Cannon", modules=["Guardian Cannon"],
    ))

    assert result["modules"] == []
    assert result["purchase_restrictions"][0]["unlock_status"] == "unknown"
    assert "Verify the exact module" in result["warnings"][0]


def test_unlock_resources_in_stock_are_not_an_unlock_confirmation(tmp_path):
    from types import SimpleNamespace

    plugin = _guardian_purchase_route(tmp_path, mixed=False)
    product = next(product for product in plugin._broker_products
                   if product["broker_id"] == "guardian_module_reinforcement_package")
    states = {
        "Materials": {"LastUpdated": "today", "Manufactured": [
            {"Name": requirement["covas_id"], "Count": requirement["quantity"]}
            for requirement in product["requirements"] if requirement["type"] != "commodity"
        ]},
        "Cargo": {"timestamp": "today", "Vessel": "Ship", "Inventory": [
            {"Name": "reinforcedmountingplate", "Count": 9},
        ]},
    }
    plugin.helper._event_manager = SimpleNamespace(get_current_state=lambda: ([], states))

    result = json.loads(plugin._next_stop_action(SessionActionParams(), {}))

    assert result["status"] == "blocked"
    assert result["purchase_restrictions"][0]["unlock_status"] == "locked"


def test_prepare_jump_resumes_saved_route_even_after_moving(tmp_path, monkeypatch):
    plugin, _ = _tracked_route(tmp_path)
    key = ("F1", "1")
    original = deepcopy(plugin._route_records[key])
    expected = next(action["system"] for action in original["actions"] if action["kind"] == "engineer_visit")
    monkeypatch.setattr(plugin, "_build_advice_action", lambda *_: pytest.fail("Unexpected recalculation"))
    prepare = Mock(side_effect=_navigation_result)
    plugin.helper.prepare_navigation = prepare
    plugin._handle_ship_event({
        "event": "FSDJump", "StarSystem": "Somewhere else", "StarPos": [20, 30, 40],
    })

    reply = json.loads(plugin._prepare_next_jump_action(PrepareJumpParams(), {}))

    assert reply["status"] == "ready"
    assert reply["calculated"] is False
    assert reply["system"] == expected
    prepare.assert_called_once_with(expected)
    assert plugin._route_records[key]["calculated_at"] == original["calculated_at"]
    assert plugin._route_records[key]["actions"] == original["actions"]


def test_prepare_jump_calculates_missing_route_then_navigates_in_one_action(tmp_path, monkeypatch):
    plugin, _ = _tracked_route(tmp_path)
    plugin._route_records.clear()
    calculation = Mock(wraps=plugin._build_advice_action)
    monkeypatch.setattr(plugin, "_build_advice_action", calculation)
    prepare = Mock(side_effect=_navigation_result)
    plugin.helper.prepare_navigation = prepare

    first = json.loads(plugin._prepare_next_jump_action(PrepareJumpParams(), {}))
    second = json.loads(plugin._prepare_next_jump_action(PrepareJumpParams(), {}))

    assert first["status"] == second["status"] == "ready"
    assert first["calculated"] is True and second["calculated"] is False
    assert calculation.call_count == 1
    assert prepare.call_count == 2


def test_prepare_jump_recalculates_only_when_explicitly_requested(tmp_path, monkeypatch):
    plugin, _ = _tracked_route(tmp_path)
    calculation = Mock(wraps=plugin._build_advice_action)
    monkeypatch.setattr(plugin, "_build_advice_action", calculation)
    plugin.helper.prepare_navigation = Mock(side_effect=_navigation_result)

    reply = json.loads(plugin._prepare_next_jump_action(PrepareJumpParams(recalculate=True), {}))

    assert reply["status"] == "ready"
    assert reply["recalculated"] is True
    calculation.assert_called_once()


def test_failed_recalculation_never_uses_the_old_route_as_a_success(tmp_path, monkeypatch):
    plugin, _ = _tracked_route(tmp_path)
    monkeypatch.setattr(plugin, "_build_advice_action", lambda *_: "The ship state is unavailable.")
    plugin.helper.prepare_navigation = Mock()

    result = json.loads(plugin._prepare_next_jump_action(PrepareJumpParams(recalculate=True), {}))

    assert result == {"status": "blocked", "message": "The ship state is unavailable."}
    plugin.helper.prepare_navigation.assert_not_called()


def test_prepare_jump_uses_purchase_stop_before_engineers(tmp_path):
    plugin = _plugin(tmp_path)
    plugin._handle_ship_event({"event": "LoadGame", "FID": "F1", "ShipID": 1, "Ship": "python"})
    plugin.import_plan("python", "Shopping", {"steps": [
        {"id": "beam", "slot": "LargeHardpoint1", "item": "hpt_beamlaser_fixed_large",
         "engineering": {"BlueprintName": "Weapon_LongRange", "Level": 5}},
    ]})
    plugin.start_session("Shopping", "1")
    plugin._handle_ship_event({"event": "Loadout", "Ship": "python", "ShipID": 1, "Modules": []})
    plugin._current_position = (0, 0, 0)
    plugin.helper.search_stations_for_modules = lambda filters, **kwargs: {
        "status": "ok", "stations": [{
            "id": "shop", "name": "Al Saud Terminal", "system": "Wu Kuku",
            "coords": {"x": 10, "y": 0, "z": 0}, "matched_filters": filters,
        }],
    }
    prepare = Mock(side_effect=_navigation_result)
    plugin.helper.prepare_navigation = prepare

    reply = json.loads(plugin._prepare_next_jump_action(PrepareJumpParams(), {}))

    assert reply["status"] == "ready"
    assert reply["kind"] == "station_purchase"
    assert reply["system"] == "Wu Kuku"
    assert reply["station"] == "Al Saud Terminal"
    assert reply["distance_ly"] == 10
    prepare.assert_called_once_with("Wu Kuku")


def test_reading_next_stop_does_not_prepare_navigation_or_recalculate(tmp_path, monkeypatch):
    plugin, _ = _tracked_route(tmp_path)
    monkeypatch.setattr(plugin, "_build_advice_action", lambda *_: pytest.fail("Unexpected recalculation"))
    plugin.helper.prepare_navigation = Mock()

    reply = json.loads(plugin._next_stop_action(SessionActionParams(), {}))

    assert reply["status"] == "next_stop"
    assert "not in-game navigation" in reply["response_policy"]
    plugin.helper.prepare_navigation.assert_not_called()


@pytest.mark.parametrize("status", ["arrived", "blocked", "failed"])
def test_prepare_jump_relays_navigation_failure_or_local_work_without_skipping(tmp_path, status):
    plugin, _ = _tracked_route(tmp_path)
    prepare = Mock(side_effect=lambda system: {
        **_navigation_result(system, status=status),
        "active_destination": "Wrong destination" if status == "failed" else None,
    })
    plugin.helper.prepare_navigation = prepare

    reply = json.loads(plugin._prepare_next_jump_action(PrepareJumpParams(), {}))

    assert reply["status"] == status
    if status == "failed":
        assert reply["navigation"]["active_destination"] == "Wrong destination"
    assert "do not claim the destination is unreachable" in reply["response_policy"]
    prepare.assert_called_once()


def test_prepare_jump_does_not_navigate_after_route_completion(tmp_path):
    plugin, _ = _tracked_route(tmp_path)
    _craft_route(plugin, 3)
    plugin.helper.prepare_navigation = Mock()

    assert json.loads(plugin._prepare_next_jump_action(PrepareJumpParams(), {}))["status"] == "complete"
    plugin.helper.prepare_navigation.assert_not_called()


def test_prepare_jump_reports_missing_host_api_instead_of_just_calculating(tmp_path, monkeypatch):
    plugin, _ = _tracked_route(tmp_path)
    monkeypatch.setattr(plugin, "_build_advice_action", lambda *_: pytest.fail("Unexpected calculation"))
    result = json.loads(plugin._prepare_next_jump_action(PrepareJumpParams(), {}))
    assert result["status"] == "blocked"
    assert "host navigation-preparation API is unavailable" in result["message"]


def test_prepare_jump_blocks_changed_target_plan(tmp_path):
    plugin, _ = _tracked_route(tmp_path)
    plugin.import_plan("python", "Tracked route", {"steps": [
        {"id": "fsd", "slot": "FrameShiftDrive", "item": "int_hyperdrive_size4_class5",
         "engineering": {"BlueprintName": "FSD_LongRange", "Level": 5}},
    ]})
    plugin.helper.prepare_navigation = Mock()

    result = json.loads(plugin._prepare_next_jump_action(PrepareJumpParams(), {}))

    assert result["status"] == "blocked"
    assert "recalculate" in result["message"]
    plugin.helper.prepare_navigation.assert_not_called()


def test_prepare_jump_blocks_an_explicit_session_for_a_different_ship(tmp_path):
    plugin, _ = _tracked_route(tmp_path)
    session_id = plugin.get_session()["id"]
    plugin._handle_ship_event({"event": "Loadout", "ShipID": 2, "Ship": "python", "Modules": []})
    plugin.helper.prepare_navigation = Mock()

    result = json.loads(plugin._prepare_next_jump_action(PrepareJumpParams(session_id=session_id), {}))

    assert result["status"] == "blocked"
    assert "another ship" in result["message"]
    plugin.helper.prepare_navigation.assert_not_called()


def test_prepare_jump_blocks_a_regressed_loadout_without_recalculating(tmp_path, monkeypatch):
    plugin, _ = _tracked_route(tmp_path)
    _craft_route(plugin, 2)
    plugin._handle_ship_event({
        "event": "Loadout", "ShipID": 1, "Ship": "python",
        "Modules": [{"Slot": "FrameShiftDrive", "Item": "int_hyperdrive_size4_class5"}],
    })
    monkeypatch.setattr(plugin, "_build_advice_action", lambda *_: pytest.fail("Unexpected recalculation"))
    plugin.helper.prepare_navigation = Mock()

    result = json.loads(plugin._prepare_next_jump_action(PrepareJumpParams(), {}))

    assert result["status"] == "blocked"
    plugin.helper.prepare_navigation.assert_not_called()


def test_prepare_jump_restores_a_legacy_snapshot_without_remaining_actions(tmp_path, monkeypatch):
    plugin, _ = _tracked_route(tmp_path)
    record = plugin._route_records[("F1", "1")]
    record.pop("remaining_actions")
    with plugin.get_db() as db:
        db.execute("UPDATE upgrade_routes SET snapshot = ?", (json.dumps(record),))
    restored = _plugin(tmp_path)
    restored._handle_ship_event({"event": "LoadGame", "FID": "F1", "ShipID": 1, "Ship": "python"})
    monkeypatch.setattr(restored, "_build_advice_action", lambda *_: pytest.fail("Unexpected recalculation"))
    restored.helper.prepare_navigation = Mock(side_effect=_navigation_result)

    result = json.loads(restored._prepare_next_jump_action(PrepareJumpParams(), {}))

    assert result["status"] == "ready"
    assert result["calculated"] is False


def test_route_summary_preserves_every_stop_and_uses_current_position_distance(tmp_path):
    from plugins.ship_upgrade_manager.planner import PlannedAction, analyze_build

    plugin = _plugin(tmp_path)
    plugin._current_position = (0, 0, 0)
    actions = [
        PlannedAction("station_purchase", None, "Beam Laser", system="Wu Kuku",
                      station="Al Saud Terminal", coords=(10, 0, 0), modules=["Beam Laser"]),
        PlannedAction("material_trade", None, "Trader", system="Trade", station="Exchange"),
        PlannedAction("broker_visit", None, "Broker", system="Unlock", station="Broker Station"),
        PlannedAction("engineer_visit", None, "Tod McQuinn", system="Wolf 397",
                      station="Trophy Camp", leg_distance_ly=95.3),
    ]
    deltas = analyze_build([{"slot": "LargeHardpoint1", "item": "hpt_beamlaser_fixed_large"}], [])

    summary = plugin._route_summary(actions, deltas)

    assert "Next stop: Wu Kuku / Al Saud Terminal (station_purchase), 10.0 ly from the current position" in summary
    stops = summary.split("Stops in order: ", 1)[1]
    assert stops.index("Al Saud Terminal") < stops.index("Exchange") < stops.index("Broker Station") < stops.index("Tod McQuinn")
    assert "95.3" not in summary
    assert "has not plotted in-game navigation" in summary


def test_route_restored_after_restart_and_isolated_by_commander_and_ship(tmp_path, monkeypatch):
    plugin, _ = _tracked_route(tmp_path, experimental=True)
    original = plugin._route_records[("F1", "1")]["fields"]
    restored = _plugin(tmp_path)
    monkeypatch.setattr(restored, "_build_advice_action", lambda *args: pytest.fail("Unexpected recalculation"))
    restored._handle_ship_event({"event": "LoadGame", "FID": "F2", "ShipID": 1, "Ship": "python"})
    assert "plugin.sum.route.empty" in _route_kinds(restored)
    restored._handle_ship_event({"event": "LoadGame", "FID": "F1", "ShipID": 2, "Ship": "python"})
    assert "plugin.sum.route.empty" in _route_kinds(restored)
    restored._handle_ship_event({"event": "LoadGame", "FID": "F1", "ShipID": 1, "Ship": "python"})
    assert restored._field("route", "route_summary")["params"]["modules"] == 1
    assert any(field.get("params", {}).get("target") == "Deep Charge" for field in original)
    assert "plugin.sum.route.engineer_visit" in _route_kinds(restored)
    restored.on_chat_stop(restored.helper)
    restored.on_chat_start(_Helper(tmp_path))
    assert "plugin.sum.route.engineer_visit" in _route_kinds(restored)


def test_route_prunes_completed_grades_and_stops_without_search(tmp_path, monkeypatch):
    plugin, _ = _tracked_route(tmp_path, experimental=True)
    monkeypatch.setattr(plugin, "_build_advice_action", lambda *args: pytest.fail("Unexpected recalculation"))
    plugin.helper.search_stations_for_modules = lambda *args, **kwargs: pytest.fail("Unexpected station search")
    _craft_route(plugin, 2)
    actions = [field for field in plugin._grid("route")["fields"] if field["key"].startswith("route_action_")]
    assert all(field.get("params", {}).get("grade") != 2 for field in actions)
    _craft_route(plugin, 3)
    assert plugin.get_session()["completed_steps"] == []
    assert "plugin.sum.route.experimental" in _route_kinds(plugin)
    assert not any("applyBlueprint" in kind or "apply_blueprint" in kind for kind in _route_kinds(plugin) if kind)
    _craft_route(plugin, 3, ApplyExperimentalEffect="special_fsd_fuelcapacity")
    assert plugin.get_session()["completed_steps"] == ["fsd"]
    assert plugin._field("route", "route_summary")["params"]["modules"] == 0
    assert "plugin.sum.route.engineer_visit" not in _route_kinds(plugin)
    assert "plugin.sum.route.complete" in _route_kinds(plugin)
    restored = _plugin(tmp_path)
    restored._current_fid, restored._current_ship_id = "F1", "1"
    restored._publish_status()
    assert "plugin.sum.route.complete" in _route_kinds(restored)


def test_manual_step_completion_updates_saved_route(tmp_path):
    plugin, _ = _tracked_route(tmp_path)
    plugin.complete_step("fsd", "Confirmed by commander")
    assert "plugin.sum.route.complete" in _route_kinds(plugin)
    assert plugin._field("route", "route_summary")["params"]["modules"] == 0


@pytest.mark.parametrize("change", ["ModuleStore", "ModuleSell", "Loadout", "EngineerCraft"])
def test_route_loadout_regression_blocks_then_resumes(tmp_path, change):
    plugin, _ = _tracked_route(tmp_path)
    _craft_route(plugin, 2)
    previous = json.loads(json.dumps(plugin._current_loadout_modules))
    plugin.helper.events.clear()
    if change == "Loadout":
        event = {"event": change, "ShipID": 1, "Ship": "python", "Modules": []}
    elif change == "EngineerCraft":
        event = {"event": change, "Slot": "FrameShiftDrive", "Module": "int_hyperdrive_size4_class5",
                 "BlueprintName": "FSD_FastBoot", "Level": 1, "Quality": 1}
    else:
        event = {"event": change, "ShipID": 1, "Slot": "FrameShiftDrive", "StoredItem": "int_hyperdrive_size4_class5"}
    plugin._handle_ship_event(event)
    assert "plugin.sum.route.loadoutBlocked" in _route_kinds(plugin)
    assert "cannot be completed" in plugin.helper.events[-1].plugin_event_content
    plugin._handle_ship_event(event)
    assert len(plugin.helper.events) == 1
    plugin._handle_ship_event({"event": "Loadout", "ShipID": 1, "Ship": "python", "Modules": previous})
    assert "plugin.sum.route.loadoutBlocked" not in _route_kinds(plugin)
    assert "resumed" in plugin.helper.events[-1].plugin_event_content
    assert plugin.get_session()["plan_name"] == "Tracked route"


def test_completed_module_removed_does_not_leave_route_complete(tmp_path):
    plugin, _ = _tracked_route(tmp_path)
    _craft_route(plugin, 3)
    assert "plugin.sum.route.complete" in _route_kinds(plugin)
    plugin._handle_ship_event({"event": "ModuleStore", "ShipID": 1, "Slot": "FrameShiftDrive"})
    assert "plugin.sum.route.loadoutBlocked" in _route_kinds(plugin)
    assert "plugin.sum.route.complete" not in _route_kinds(plugin)
    assert plugin._field("route", "route_summary")["params"]["modules"] == 1
    assert plugin.get_session()["completed_steps"] == []
    restored = _plugin(tmp_path)
    restored._handle_ship_event({"event": "LoadGame", "FID": "F1", "ShipID": 1, "Ship": "python"})
    assert "plugin.sum.route.loadoutBlocked" in _route_kinds(restored)
    assert "plugin.sum.route.complete" not in _route_kinds(restored)


def test_route_swap_preserves_plan_and_announces_resume(tmp_path):
    plugin, _ = _tracked_route(tmp_path)
    plugin.helper.events.clear()
    plugin._handle_ship_event({"event": "ShipyardSwap", "ShipID": 2, "ShipType": "anaconda"})
    assert "plugin.sum.route.empty" in _route_kinds(plugin)
    assert "suspended" in plugin.helper.events[-1].plugin_event_content
    plugin._handle_ship_event({"event": "ShipyardSwap", "ShipID": 1, "ShipType": "python"})
    assert "resumed" in plugin.helper.events[-1].plugin_event_content
    assert "plugin.sum.route.engineer_visit" in _route_kinds(plugin)
    assert len(plugin.list_sessions()) == 1


def test_plan_change_requires_recalculation_and_delete_removes_saved_route(tmp_path):
    plugin, plan_id = _tracked_route(tmp_path)
    plugin.import_plan("python", "Tracked route", {"steps": [{"id": "new", "slot": "FrameShiftDrive", "item": "int_hyperdrive_size5_class5"}]})
    assert "plugin.sum.route.planChanged" in _route_kinds(plugin)
    plugin._delete_plan_by_id(plan_id)
    assert "plugin.sum.route.empty" in _route_kinds(plugin)
    with plugin.get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM upgrade_routes").fetchone()[0] == 0


def test_route_deviation_notifies_once_and_updates_leg_without_reordering(tmp_path):
    plugin, _ = _tracked_route(tmp_path)
    record = plugin._route_records[("F1", "1")]
    for action in record["actions"]:
        if action["kind"] == "engineer_visit":
            action["coords"] = [100, 0, 0]
    plugin.helper.events.clear()
    for pos in ([50, 10, 0], [50, 100, 0], [50, 110, 0]):
        plugin._handle_ship_event({"event": "FSDJump", "StarSystem": "Away", "StarPos": pos})
    assert len(plugin.helper.events) == 1
    assert "significantly away" in plugin.helper.events[0].plugin_event_content
    assert "plugin.sum.route.offRoute" in _route_kinds(plugin)
    plugin._handle_ship_event({"event": "FSDJump", "StarSystem": "On route", "StarPos": [50, 0, 0]})
    assert "plugin.sum.route.offRoute" not in _route_kinds(plugin)
    assert plugin._field("route", "route_leg_0")["params"]["distance"] == 50


def test_historic_events_before_calculation_do_not_regress_saved_route(tmp_path):
    plugin, _ = _tracked_route(tmp_path)
    original = plugin._route_records[("F1", "1")]["fields"]
    plugin._on_event(GameEvent(content={"event": "Loadout", "timestamp": "2020-01-01T00:00:00Z", "ShipID": 1,
                                      "Ship": "python", "Modules": []}, historic=True), {})
    assert plugin._route_records[("F1", "1")]["fields"] == original
    assert plugin.helper.events == []


def test_route_material_consumption_uses_remaining_work_and_resumes_after_collection(tmp_path):
    from plugins.ship_upgrade_manager.planner import PlannedAction, analyze_build

    plugin, _ = _tracked_route(tmp_path)
    target = plugin._upgrade_target()
    plugin._publish_route([
        PlannedAction("engineer_visit", None, "Engineer", coords=(100, 0, 0)),
        PlannedAction("apply_blueprint", "frameshiftdrive", "FSD_LongRange", grade=1, increments=1,
                      materials=[{"material_id": "iron", "quantity": 1}], stock_status="sufficient"),
        PlannedAction("apply_blueprint", "frameshiftdrive", "FSD_LongRange", grade=2, increments=2,
                      materials=[{"material_id": "iron", "quantity": 1}], stock_status="sufficient"),
        PlannedAction("apply_blueprint", "frameshiftdrive", "FSD_LongRange", grade=3, increments=3,
                      materials=[{"material_id": "iron", "quantity": 1}], stock_status="sufficient"),
    ], analyze_build(target["steps"], plugin._current_loadout_modules), target, None)
    plugin._handle_ship_event({"event": "Materials", "Raw": [{"Name": "iron", "Count": 6}]})
    plugin.helper.events.clear()
    _craft_route(plugin, 1, Ingredients=[{"Name": "iron", "Count": 1}])
    assert "plugin.sum.route.resourcesMissing" not in _route_kinds(plugin)
    assert plugin.helper.events == []
    plugin._handle_ship_event({"event": "Synthesis", "Materials": [{"Name": "iron", "Count": 2}]})
    assert "plugin.sum.route.resourcesMissing" in _route_kinds(plugin)
    assert plugin._route_records[("F1", "1")]["shortages"] == {"Iron": 2}
    assert len(plugin.helper.events) == 1
    plugin._handle_ship_event({"event": "MaterialCollected", "Name": "iron", "Category": "Raw", "Count": 2})
    assert "plugin.sum.route.resourcesMissing" not in _route_kinds(plugin)
    assert "resumed" in plugin.helper.events[-1].plugin_event_content
    plugin._handle_ship_event({"event": "MaterialDiscarded", "Name": "iron", "Category": "Raw", "Count": 1})
    assert plugin._route_records[("F1", "1")]["shortages"] == {"Iron": 1}


def test_route_commodity_spending_and_broker_unlock_update_without_search(tmp_path, monkeypatch):
    plugin = _plugin(tmp_path)
    plugin._current_fid = "F1"
    plugin._broker_set_status("guardian_module_reinforcement_package", "locked")
    item = "int_guardianmodulereinforcement_size2_class1"
    plugin.import_plan("python", "Broker route", {"steps": [{"id": "mrp", "slot": "Slot1", "item": item}]})
    plugin.start_session("Broker route", "1")
    product = next(product for product in plugin._broker_products if product["broker_id"] == "guardian_module_reinforcement_package")
    states = {
        "Loadout": {"ShipID": 1, "Ship": "python", "Modules": []},
        "Location": {"StarSystem": "Sol", "StarPos": [0, 0, 0]},
        "Materials": {"LastUpdated": "today", "Manufactured": [
            {"Name": requirement["covas_id"], "Count": requirement["quantity"]}
            for requirement in product["requirements"] if requirement["type"] != "commodity"
        ]},
        "Cargo": {"timestamp": "today", "Vessel": "Ship", "Inventory": [{"Name": "reinforcedmountingplate", "Count": 9}]},
    }
    class EventManager:
        def get_current_state(self):
            return [], states
    plugin.helper._event_manager = EventManager()
    plugin.helper.search_stations_for_modules = lambda filters, **kwargs: {
        "status": "ok", "stations": [{"name": "Purchase", "system": "Far", "id": "purchase",
                                      "coords": {"x": 100, "y": 0, "z": 0}, "matched_filters": filters}],
    }
    monkeypatch.setattr(plugin, "_route_broker_search", lambda *args: {
        "status": "ok", "stations": [{"name": "Broker", "system": "Path", "coords": {"x": 50, "y": 0, "z": 0}}],
    })
    plugin._build_advice_action()
    monkeypatch.setattr(plugin, "_route_broker_search", lambda *args: pytest.fail("Unexpected broker search"))
    plugin._handle_ship_event({"event": "MarketSell", "Type": "reinforcedmountingplate", "Count": 2})
    assert "plugin.sum.route.resourcesMissing" in _route_kinds(plugin)
    assert plugin._route_records[("F1", "1")]["shortages"] == {"Reinforced Mounting Plate": 2}
    plugin._handle_ship_event({"event": "MarketBuy", "Type": "reinforcedmountingplate", "Count": 2})
    assert "plugin.sum.route.resourcesMissing" not in _route_kinds(plugin)
    plugin._handle_ship_event({"event": "TechnologyBroker", "ItemsUnlocked": [{"Name": item}],
                               "Commodities": [{"Name": "reinforcedmountingplate", "Count": 9}]})
    assert "plugin.sum.route.broker_visit" not in _route_kinds(plugin)
    assert "plugin.sum.route.purchaseLocked" not in _route_kinds(plugin)
    assert "plugin.sum.route.station_purchase" in _route_kinds(plugin)
    assert "plugin.sum.route.resourcesMissing" not in _route_kinds(plugin)
    plugin._handle_ship_event({"event": "ModuleBuy", "ShipID": 1, "Slot": "Slot1", "BuyItem": item})
    assert "plugin.sum.route.complete" in _route_kinds(plugin)


def test_route_planned_material_trade_removes_stop_once(tmp_path):
    from plugins.ship_upgrade_manager.planner import PlannedAction, analyze_build
    plugin, _ = _tracked_route(tmp_path)
    target = plugin._upgrade_target()
    plugin._publish_route([
        PlannedAction("material_trade", None, "Raw material trader", coords=(10, 0, 0), materials=[{
            "give_id": "carbon", "give_name": "Carbon", "give_quantity": 6,
            "receive_id": "iron", "receive_name": "Iron", "receive_quantity": 1,
        }]),
        PlannedAction("engineer_visit", None, "Engineer", coords=(100, 0, 0)),
        PlannedAction("apply_blueprint", "frameshiftdrive", "FSD_LongRange", grade=3,
                      materials=[{"material_id": "iron", "quantity": 1}]),
    ], analyze_build(target["steps"], plugin._current_loadout_modules), target, None)
    trade = {"event": "MaterialTrade", "Paid": {"Material": "carbon", "Quantity": 6},
             "Received": {"Material": "iron", "Quantity": 1}}
    plugin._handle_ship_event(trade)
    plugin._handle_ship_event(trade)
    assert "plugin.sum.route.material_trade" not in _route_kinds(plugin)
    assert "plugin.sum.route.engineer_visit" in _route_kinds(plugin)
    assert plugin._route_records[("F1", "1")]["trades"] == [["carbon", "iron", 1]]


def test_route_updates_silently_during_historic_replay(tmp_path):
    plugin, _ = _tracked_route(tmp_path)
    plugin._on_event(GameEvent(content={"event": "EngineerCraft", "Slot": "FrameShiftDrive",
                                      "Module": "int_hyperdrive_size4_class5", "BlueprintName": "FSD_LongRange",
                                      "Level": 3, "Quality": 1}, historic=True), {})
    assert "plugin.sum.route.complete" in _route_kinds(plugin)
    assert plugin.helper.events == []


def test_route_two_events_in_same_second_are_applied_but_not_replayed_twice(tmp_path):
    plugin, _ = _tracked_route(tmp_path)
    first = {"event": "EngineerCraft", "timestamp": "2099-01-01T00:00:00Z", "Slot": "FrameShiftDrive",
             "Module": "int_hyperdrive_size4_class5", "BlueprintName": "FSD_LongRange", "Level": 2, "Quality": 1}
    second = {**first, "Level": 3}
    for event in (first, second):
        plugin._on_event(GameEvent(content=event, historic=True), {})
    assert "plugin.sum.route.complete" in _route_kinds(plugin)
    for event in (first, second):
        plugin._on_event(GameEvent(content=event, historic=True), {})
    assert "plugin.sum.route.complete" in _route_kinds(plugin)
    assert "plugin.sum.route.loadoutBlocked" not in _route_kinds(plugin)


def test_route_is_not_restored_without_identified_commander(tmp_path):
    plugin, _ = _tracked_route(tmp_path)
    plugin._current_fid = ""
    plugin._build_advice_action()
    restored = _plugin(tmp_path)
    assert ("", "1") not in restored._route_records
    assert ("F1", "1") in restored._route_records


def test_engineer_progress_does_not_block_or_recalculate_saved_route(tmp_path, monkeypatch):
    plugin, _ = _tracked_route(tmp_path)
    original_fields = json.loads(json.dumps(plugin._grid("route")["fields"]))
    monkeypatch.setattr(plugin, "_build_advice_action", lambda *args: pytest.fail("Unexpected recalculation"))
    name = next(action["target"] for action in plugin._route_records[("F1", "1")]["actions"] if action["kind"] == "engineer_visit")
    progress = {"Engineers": [{"Engineer": name, "Progress": "Barred"}]}
    class EventManager:
        def get_current_state(self):
            return [], {"EngineerProgress": progress}
    plugin.helper._event_manager = EventManager()
    plugin._handle_ship_event({"event": "EngineerProgress"})
    assert plugin._grid("route")["fields"] == original_fields
    plugin._handle_ship_event({"event": "FSDJump", "StarSystem": "Sol", "StarPos": [0, 0, 0]})
    assert "plugin.sum.route.engineerBlocked" not in _route_kinds(plugin)
    assert plugin.helper.events == []
    refreshed_fields = json.loads(json.dumps(plugin._grid("route")["fields"]))
    progress["Engineers"][0]["Progress"] = "Unlocked"
    plugin._handle_ship_event({"event": "EngineerProgress"})
    assert plugin._grid("route")["fields"] == refreshed_fields
    assert plugin.helper.events == []


def test_restored_route_discards_removed_engineer_access_warning(tmp_path):
    plugin, _ = _tracked_route(tmp_path)
    record = plugin._route_records[("F1", "1")]
    record["flags"] = record["notified_flags"] = ["engineerBlocked"]
    record["fields"].append(plugin._paragraph("route_status_engineerBlocked", "plugin.sum.route.engineerBlocked"))
    plugin._save_route(("F1", "1"), record)
    restored = _plugin(tmp_path)
    restored._handle_ship_event({"event": "LoadGame", "FID": "F1", "ShipID": 1, "Ship": "python"})
    assert "plugin.sum.route.engineerBlocked" not in _route_kinds(restored)
    assert restored._route_records[("F1", "1")]["flags"] == []
    assert restored.helper.events == []


def test_saved_route_cargo_transfers_and_new_purchases_restore_inventory(tmp_path):
    plugin, _ = _tracked_route(tmp_path)
    record = plugin._route_records[("F1", "1")]
    record["Cargo"] = {"timestamp": "known", "Vessel": "Ship", "Inventory": [{"Name": "gold", "Count": 2}]}
    plugin._handle_ship_event({"event": "CargoTransfer", "Transfers": [{"Type": "gold", "Count": 2, "Direction": "fromship"}]})
    assert record["Cargo"]["Inventory"][0]["Count"] == 0
    plugin._handle_ship_event({"event": "CargoTransfer", "Transfers": [{"Type": "gold", "Count": 1, "Direction": "toship"}]})
    assert record["Cargo"]["Inventory"][0]["Count"] == 1
    plugin._handle_ship_event({"event": "MarketBuy", "Type": "silver", "Count": 3})
    assert next(item["Count"] for item in record["Cargo"]["Inventory"] if item["Name"] == "silver") == 3


def test_restart_module_info_keeps_saved_engineering_until_loadout(tmp_path):
    plugin, _ = _tracked_route(tmp_path)
    _craft_route(plugin, 2)
    restored = _plugin(tmp_path)
    restored._handle_ship_event({"event": "LoadGame", "FID": "F1", "ShipID": 1, "Ship": "python"})
    restored._handle_ship_event({"event": "ModuleInfo", "Modules": [{"Slot": "FrameShiftDrive", "Item": "int_hyperdrive_size4_class5"}]})
    assert "plugin.sum.route.loadoutBlocked" not in _route_kinds(restored)
    assert restored._current_loadout_modules[0]["Engineering"]["Level"] == 2


def test_route_restored_from_runtime_identity_without_new_journal_events(tmp_path, monkeypatch):
    from lib.projections.commander import CommanderStateModel
    from lib.projections.loadout import LoadoutState

    _tracked_route(tmp_path)
    restored = _plugin(tmp_path, running=False)
    helper = _Helper(tmp_path)
    class EventManager:
        def get_current_state(self):
            return [], {"Commander": CommanderStateModel(FID="F1"),
                        "Loadout": LoadoutState(ShipID=1, Ship="python")}
    helper._event_manager = EventManager()
    monkeypatch.setattr(restored, "_build_advice_action", lambda *args: pytest.fail("Unexpected recalculation"))
    restored.on_chat_start(helper)
    assert "plugin.sum.route.empty" not in _route_kinds(restored)
    assert "plugin.sum.route.engineer_visit" in _route_kinds(restored)
    assert restored._current_fid == "F1"
    assert restored._current_ship_id == "1"


def test_calculated_route_saved_with_projected_commander_without_loadgame(tmp_path):
    plugin, _ = _tracked_route(tmp_path)
    with plugin.get_db() as db:
        db.execute("DELETE FROM upgrade_routes")
    plugin._current_fid = ""
    class EventManager:
        def get_current_state(self):
            return [], {"Commander": {"FID": "F1"}}
    plugin.helper._event_manager = EventManager()
    plugin._build_advice_action()
    with plugin.get_db() as db:
        rows = db.execute("SELECT fid, ship_instance_id FROM upgrade_routes").fetchall()
    assert [(row["fid"], row["ship_instance_id"]) for row in rows] == [("F1", "1")]
    assert ("", "1") not in plugin._route_records


def test_commander_event_restores_route_without_loadgame(tmp_path):
    plugin, _ = _tracked_route(tmp_path)
    plugin._handle_ship_event({"event": "LoadGame", "FID": "F2", "ShipID": 1, "Ship": "python"})
    assert "plugin.sum.route.empty" in _route_kinds(plugin)
    plugin._handle_ship_event({"event": "Commander", "FID": "F1"})
    assert "plugin.sum.route.engineer_visit" in _route_kinds(plugin)
    plugin._handle_ship_event({"event": "Commander", "FID": "F2"})
    assert "plugin.sum.route.empty" in _route_kinds(plugin)


def test_route_identity_recovered_from_commander_journal_without_loadgame(tmp_path, monkeypatch):
    import plugins.ship_upgrade_manager.ship_upgrade_manager as module

    _tracked_route(tmp_path)
    journals = tmp_path / "journals"
    journals.mkdir()
    (journals / "Journal.test.log").write_text("\n".join(json.dumps(entry) for entry in [
        {"event": "Commander", "FID": "F1"},
        {"event": "Loadout", "ShipID": 1, "Ship": "python", "Modules": [{"Slot": "FrameShiftDrive", "Item": "int_hyperdrive_size4_class5"}]},
    ]), encoding="utf-8")
    monkeypatch.setattr(module, "get_ed_journals_path", lambda _config: str(journals))
    restored = _plugin(tmp_path)
    assert restored._current_fid == "F1"
    assert "plugin.sum.route.engineer_visit" in _route_kinds(restored)


def test_runtime_commander_takes_priority_over_previous_journal_context(tmp_path, monkeypatch):
    _tracked_route(tmp_path)
    restored = _plugin(tmp_path, running=False)
    monkeypatch.setattr(restored, "_recover_broker_history", lambda: setattr(restored, "_current_fid", "F2"))
    helper = _Helper(tmp_path)
    class EventManager:
        def get_current_state(self):
            return [], {"Commander": {"FID": "F1"}, "Loadout": {"ShipID": 1, "Ship": "python"}}
    helper._event_manager = EventManager()
    restored.on_chat_start(helper)
    assert "plugin.sum.route.engineer_visit" in _route_kinds(restored)
    assert restored._current_fid == "F1"


@pytest.mark.parametrize("fid", [None, "", "Unknown", " none "])
def test_unknown_runtime_commander_does_not_select_another_commanders_route(tmp_path, fid):
    _tracked_route(tmp_path)
    restored = _plugin(tmp_path, running=False)
    helper = _Helper(tmp_path)
    class EventManager:
        def get_current_state(self):
            return [], {"Commander": {"FID": fid}, "Loadout": {"ShipID": 1, "Ship": "python"}}
    helper._event_manager = EventManager()
    restored.on_chat_start(helper)
    assert "plugin.sum.route.empty" in _route_kinds(restored)
    assert restored._current_fid == ""
