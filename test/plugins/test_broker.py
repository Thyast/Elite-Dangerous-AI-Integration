import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from lib.PluginBase import PluginManifest
from lib.projections.cargo import Cargo
from lib.Event import GameEvent
from plugins.ship_upgrade_manager.broker import match_product, product_from_journal, recipe_needs
from plugins.ship_upgrade_manager.ship_upgrade_manager import (
    BrokerConfirmParams,
    BrokerQueryParams,
    ShipUpgradeManagerPlugin,
)


CATALOG = json.loads(
    (Path(__file__).parents[2] / "src/assets/technology_broker_unlocks.json").read_text(encoding="utf-8")
)["products"]
MRP = match_product(CATALOG, "gardian module reinforcement package")


def test_broker_recipe_separates_commodities_and_inventory():
    states = {
        "Materials": {
            "LastUpdated": "2026-10-04T07:00:00Z",
            "Raw": [],
            "Encoded": [{"Name": "guardian_moduleblueprint", "Count": 1}],
            "Manufactured": [{"Name": "guardian_powerconduit", "Count": 20}],
        },
        "Cargo": {
            "timestamp": "2026-10-04T07:00:00Z", "Vessel": "Ship",
            "Inventory": [{"Name": "reinforcedmountingplate", "Count": 2}],
            "TotalItems": 2, "Capacity": 16,
        },
    }
    needs = recipe_needs(MRP, states)
    assert next(n for n in needs if n["type"] == "commodity")["missing"] == 7
    assert next(n for n in needs if n["covas_id"] == "guardian_moduleblueprint")["missing"] == 0
    assert next(n for n in needs if n["covas_id"] == "guardian_powerconduit")["missing"] == 0
    assert recipe_needs(MRP, None)[-1]["missing"] is None


def test_broker_journal_matching_is_strict():
    assert product_from_journal(CATALOG, "Int_GuardianModuleReinforcement_Size2_Class1") == MRP
    assert product_from_journal(CATALOG, "guardian_hybrid_power_plant") is None
    with pytest.raises(ValueError, match="Ambiguous"):
        match_product(CATALOG, "Guardian")


class _Manager:
    def update_plugin_setting(self, *_args):
        return True

    def republish_settings(self):
        pass


class _Helper:
    _plugin_manager = _Manager()

    def __init__(self, path, states=None):
        self.path = path
        self._event_manager = self if states is not None else None
        self.states = states

    def get_plugin_data_path(self, _manifest):
        return str(self.path)

    def get_current_state(self):
        return [], self.states

    def register_projection(self, *_args):
        pass

    def register_action(self, **_kwargs):
        pass

    def register_event(self, *_args):
        pass

    def register_sideeffect(self, *_args):
        pass

    def register_status_generator(self, *_args):
        pass


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    import plugins.ship_upgrade_manager.ship_upgrade_manager as module

    journal_dir = tmp_path / "journals"
    journal_dir.mkdir()
    monkeypatch.setattr(module, "get_ed_journals_path", lambda _config: str(journal_dir))
    monkeypatch.setattr(module._JournalWatcher, "start", lambda _self: None)
    instance = ShipUpgradeManagerPlugin(PluginManifest('{"guid":"broker-test","name":"Test","version":"1.0.0"}'))
    instance.on_chat_start(_Helper(tmp_path))
    instance._current_fid = "F1"
    return instance


def test_broker_status_persists_per_commander_and_resume_target(plugin, tmp_path):
    assert "unlock status unknown" in plugin._broker_query_action(BrokerQueryParams(module=MRP["name_en"]), {})
    plugin._broker_confirm_action(BrokerConfirmParams(module=MRP["name_en"], status="locked"), {})
    assert "unlock status locked" in plugin._broker_query_action(BrokerQueryParams(), {})
    plugin._current_fid = "F2"
    assert "No Technology Broker unlock" in plugin._broker_query_action(BrokerQueryParams(), {})
    plugin._current_fid = "F1"
    plugin._handle_ship_event({"event": "TechnologyBroker", "ItemsUnlocked": [{"Name": "Int_GuardianModuleReinforcement_Size2_Class1"}]})
    assert plugin._broker_status(MRP["broker_id"]) == "unlocked"
    plugin._broker_set_status(MRP["broker_id"], "unknown")
    plugin._recover_broker_history()
    assert plugin._broker_status(MRP["broker_id"]) == "unknown"
    assert "unlock status unknown" in plugin._broker_query_action(BrokerQueryParams(), {})


def test_broker_history_does_not_infer_absent_unlocks(plugin, tmp_path, monkeypatch):
    journal = tmp_path / "journals" / "Journal.test.log"
    journal.write_text("\n".join(json.dumps(row) for row in [
        {"event": "LoadGame", "FID": "F2"},
        {"event": "TechnologyBroker", "ItemsUnlocked": [{"Name": "Int_GuardianModuleReinforcement_Size2_Class1"}]},
        {"event": "LoadGame", "FID": "F1"},
    ]), encoding="utf-8")
    plugin._recover_broker_history()
    assert plugin._broker_status(MRP["broker_id"]) == "unknown"
    plugin._current_fid = "F2"
    assert plugin._broker_status(MRP["broker_id"]) == "unlocked"


def test_broker_rechecks_cargo_after_ship_change_and_finds_candidates(plugin, monkeypatch):
    from lib.actions import actions_web

    states = {
        "Materials": {"LastUpdated": "today", "Raw": [], "Manufactured": [], "Encoded": []},
        "Cargo": {"timestamp": "today", "Vessel": "Ship", "Inventory": [
            {"Name": "reinforcedmountingplate", "Count": 2}
        ], "TotalItems": 2, "Capacity": 5, "CapacityKnown": True},
        "Location": {"StarSystem": "Sol", "StarPos": [0, 0, 0]},
    }
    plugin.helper._event_manager = plugin.helper
    plugin.helper.states = states
    requests = []

    def search(request):
        requests.append(request)
        return {"results": [{"name": "Station", "system": "Sol"}]}

    monkeypatch.setattr(actions_web, "_execute_station_search", search)
    answer = plugin._broker_query_action(BrokerQueryParams(module=MRP["name_en"], find_stations=True), {})
    assert "missing 7 (commodity)" in answer
    assert "use a ship with at least 9 t" in answer
    assert "Station (Sol)" in answer
    assert requests[0]["filters"]["market"][0]["supply"]["value"][0] == "7"
    assert requests[1]["filters"]["technology_broker"]["value"] == ["Guardian"]

    states["Cargo"] = {
        "timestamp": "later", "Vessel": "Ship", "Inventory": [],
        "TotalItems": 0, "Capacity": 16, "CapacityKnown": True,
    }
    answer = plugin._broker_query_action(BrokerQueryParams(), {})
    assert "missing 9 (commodity)" in answer
    assert "use a ship" not in answer


def test_station_search_failure_is_reported(plugin, monkeypatch):
    from lib.actions import actions_web

    plugin.helper._event_manager = plugin.helper
    plugin.helper.states = {
        "Location": {"StarSystem": "Sol"},
        "Materials": {"LastUpdated": "today", "Raw": [], "Manufactured": [], "Encoded": []},
        "Cargo": {"timestamp": "today", "Vessel": "Ship", "Inventory": [], "TotalItems": 0, "Capacity": 16},
    }

    def search(_request):
        raise ValueError("service offline")

    monkeypatch.setattr(actions_web, "_execute_station_search", search)
    result = plugin._broker_query_action(BrokerQueryParams(module=MRP["name_en"], find_stations=True), {})
    assert "station search failed (service offline)" in result
    assert "Broker station search failed (service offline)" in result


def test_cargo_keeps_internal_and_localized_names():
    cargo = Cargo()
    cargo.process(GameEvent(content={
        "event": "Cargo", "timestamp": "2026-10-04T07:00:00Z", "Vessel": "Ship",
        "Inventory": [{"Name": "reinforcedmountingplate", "Name_Localised": "Plaque de montage renforcée", "Count": 2}],
        "Count": 2,
    }, historic=False))
    assert cargo.state.Inventory[0].Name == "reinforcedmountingplate"
    assert cargo.state.Inventory[0].Name_Localised == "Plaque de montage renforcée"
    cargo.process(GameEvent(content={"event": "ShipyardSwap"}, historic=False))
    assert cargo.state.Inventory == []
    assert not cargo.state.CapacityKnown
