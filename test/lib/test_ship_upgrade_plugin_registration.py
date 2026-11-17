import sys
import json
from pathlib import Path
from queue import Queue
from unittest.mock import Mock
import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from lib.PluginHelper import PluginHelper
from lib.PluginManager import PluginManager
from lib.PluginBase import PluginManifest
from lib.ActionManager import ActionManager
from lib.actions import actions_ui
from lib.Event import GameEvent
from lib.PromptGenerator import PromptGenerator
from plugins.ship_upgrade_manager.ship_upgrade_manager import (
    ShipUpgradeManagerPlugin,
    ShipUpgradeProjection,
)


@pytest.fixture(autouse=True)
def _isolated_journal_directory(tmp_path, monkeypatch):
    import plugins.ship_upgrade_manager.ship_upgrade_manager as sum_module

    journals = tmp_path / "empty-journals"
    journals.mkdir()
    monkeypatch.setattr(sum_module, "get_ed_journals_path", lambda _config: str(journals))


class _ActionManager:
    def __init__(self):
        self.actions = []

    def registerAction(self, **kwargs):
        self.actions.append(kwargs)


def test_loadout_prompt_preserves_partial_grade_and_canonical_experimental():
    generator = PromptGenerator.__new__(PromptGenerator)
    generator.registered_status_generators = []
    generator.weapon_types = []
    generator.quest_db = Mock()
    generator.quest_db.get_all.return_value = []
    states = {"CurrentStatus": {"flags": {"InMainShip": True, "InFighter": False, "InSRV": False}, "flags2": {}}, "Loadout": {"Modules": [{
        "Slot": "PowerPlant", "Item": "int_powerplant_size5_class5",
        "Engineering": {"BlueprintName": "PowerPlant_Armoured", "Level": 5,
                        "Quality": .8, "ExperimentalEffect": "special_powerplant_cooled"},
    }]}}
    status = generator.generate_status_message(states, search_agent_context=True)
    result = yaml.safe_load(status.split("# Main Ship\n")[1].split("\n\n# ")[0])
    module = result["Loadout"]["Core Internals"][0]
    assert module["Slot"] == "PowerPlant"
    assert module["Engineering"]["Quality"] == .8
    assert module["Engineering"]["ExperimentalEffect"] == "special_powerplant_cooled"


def test_real_upgrade_view_routes_french_business_names_independently_of_gui_language(tmp_path, monkeypatch):
    cache = {}
    store = Mock()
    store.get.side_effect = cache.get
    store.set.side_effect = cache.__setitem__
    monkeypatch.setattr("lib.ActionManager.KeyValueStore", lambda _name: store)
    monkeypatch.setattr(ActionManager, "actions", {})
    monkeypatch.setattr(PluginHelper, "PLUGIN_DATA_PATH", str(tmp_path / "plugin-data"))
    monkeypatch.setattr(actions_ui, "plugin_manager", None)
    monkeypatch.setattr(actions_ui, "event_manager", None)
    messages = []
    monkeypatch.setattr(actions_ui, "send_message", messages.append)
    action_manager = ActionManager()
    actions_ui.register_ui_actions(action_manager, None)
    manager = PluginManager({"plugin_settings": {}})
    plugin = ShipUpgradeManagerPlugin(PluginManifest(
        '{"guid":"voice-upgrades","name":"Ship Upgrade","version":"1.0.0"}',
    ))
    manager.plugin_list["voice-upgrades"] = plugin
    manager.register_settings()
    actions_ui.configure_plugin_ui(manager, action_manager)
    tools = action_manager.getToolsList("mainship", False, False, True)
    commands = [
        ("affiche la progression", "session"),
        ("affiche la progression du vaisseau", "session"),
        ("affiche la progression de l'amelioration du vaisseau", "session"),
        ("Affiche-moi la progression de l'am\u00e9lioration du vaisseau.", "session"),
        ("Montre-moi les progr\u00e8s de l'am\u00e9lioration du vaisseau.", "session"),
        ("affiche le suivi des am\u00e9liorations", "session"),
        ("show ship upgrade progress", "session"),
        ("affiche les plans", "plans"),
        ("affiche les plans a debloquer", "broker"),
        ("affiche les d\u00e9blocages", "broker"),
        ("affiche la route d'am\u00e9lioration", "route"),
    ]
    for command, section in commands:
        prediction = action_manager.predict_action(command, tools)
        assert prediction is not None, command
        assert prediction[0].function.name == "showUI"
        assert json.loads(prediction[0].function.arguments) == {
            "tab": "plugin:voice-upgrades:upgrades", "submenu": section,
        }
        action_manager.runAction(prediction[0], {})
        assert messages[-1] == {"type": "ui", "show": "plugin:voice-upgrades:upgrades", "submenu": section}
    cache.clear()
    for command, section in commands:
        direct = actions_ui.predict_plugin_ui_action(command, action_manager, tools)
        assert direct is not None, command
        assert json.loads(direct[0].function.arguments) == {
            "tab": "plugin:voice-upgrades:upgrades", "submenu": section,
        }
        action_manager.runAction(direct[0], {})
        assert messages[-1] == {"type": "ui", "show": "plugin:voice-upgrades:upgrades", "submenu": section}
    description = action_manager.actions["showUI"]["tool"]["function"]["parameters"]["properties"]["tab"]["description"]
    assert "session = Progress / Progression" in description


class _EventManager:
    def __init__(self):
        self.projections = []
        self.sideeffects = []
        self.incoming = Queue()

    def register_projection(self, projection, raise_error=False):
        self.projections.append((projection, raise_error))

    def register_sideeffect(self, sideeffect):
        self.sideeffects.append(sideeffect)

    def get_current_state(self):
        return [], {}


class _PromptGenerator:
    def __init__(self):
        self.status_generators = []
        self.event_handlers = []

    def register_status_generator(self, generator):
        self.status_generators.append(generator)

    def register_prompt_event_handler(self, handler):
        self.event_handlers.append(handler)


class _Assistant:
    def __init__(self):
        self.should_reply_handlers = []

    def register_should_reply_handler(self, handler):
        self.should_reply_handlers.append(handler)


class _PluginDataHelper(PluginHelper):
    def __init__(self, *args, data_path: Path, **kwargs):
        super().__init__(*args, **kwargs)
        self.data_path = data_path

    def get_plugin_data_path(self):
        self.data_path.mkdir(parents=True, exist_ok=True)
        return str(self.data_path)


def test_ship_upgrade_plugin_registers_with_real_plugin_helper(tmp_path: Path, monkeypatch):
    import plugins.ship_upgrade_manager.ship_upgrade_manager as sum_module

    journal_dir = tmp_path / "journals"
    journal_dir.mkdir()
    monkeypatch.setattr(sum_module, "get_ed_journals_path", lambda _config: str(journal_dir))
    action_manager = _ActionManager()
    event_manager = _EventManager()
    prompt_generator = _PromptGenerator()
    helper = _PluginDataHelper(
        PluginManager({"plugin_settings": {}}),
        prompt_generator,
        {},
        action_manager,
        event_manager,
        None,
        None,
        None,
        None,
        _Assistant(),
        data_path=tmp_path,
    )
    plugin = ShipUpgradeManagerPlugin(
        PluginManifest(
            '{"guid":"registration-test-guid","name":"Ship Upgrade","version":"1.0.0"}'
        )
    )

    manager = helper._plugin_manager
    monkeypatch.setattr(PluginHelper, 'PLUGIN_DATA_PATH', str(tmp_path / 'plugin-data'))
    manager.plugin_list[plugin.plugin_manifest.guid] = plugin
    manager.register_settings()
    manager.on_chat_start(helper)

    assert isinstance(event_manager.projections[0][0], ShipUpgradeProjection)
    assert event_manager.projections[0][1] is False
    assert len(event_manager.sideeffects) == 1
    assert len(prompt_generator.status_generators) == 1
    loadout_action = next(
        action for action in action_manager.actions
        if action["name"] == "ship_upgrade_current_loadout"
    )
    assert "modules actually installed on the current ship" in loadout_action["description"]
    workshop_action = next(
        action for action in action_manager.actions
        if action["name"] == "ship_upgrade_workshop_upgrades"
    )
    assert "quelles améliorations je peux faire ici" in workshop_action["cache_prefill"]
    assert "quelle amélioration est-ce que je peux faire ici" in workshop_action["cache_prefill"]
    assert "outside the plan" in workshop_action["description"]
    assert {
        action["name"] for action in action_manager.actions
    } == {
        "ship_upgrade_start_session",
        "ship_upgrade_complete_step",
        "ship_upgrade_pause_session",
        "ship_upgrade_resume_session",
        "ship_upgrade_stop_session",
        "ship_upgrade_next_step",
        "ship_upgrade_remaining_upgrades",
        "ship_upgrade_weapon_slots",
        "ship_upgrade_workshop_upgrades",
        "ship_upgrade_list_plans",
        "ship_upgrade_preview_changes",
        "ship_upgrade_apply_changes",
        "ship_upgrade_build_advice",
        "ship_upgrade_next_stop",
        "ship_upgrade_prepare_next_jump",
        "ship_upgrade_plan_status",
        "ship_upgrade_current_loadout",
        "ship_upgrade_state",
        "ship_upgrade_goal",
        "ship_upgrade_missing_modules",
        "ship_upgrade_missing_materials",
        "ship_upgrade_material_shortfall",
        "ship_upgrade_step_materials",
        "technology_broker_unlock",
        "technology_broker_confirm",
    }

    materials_action = next(
        action for action in action_manager.actions
        if action["name"] == "ship_upgrade_missing_materials"
    )
    assert materials_action["cache_prefill"]["quels sont les matériaux manquants"] == {}
    assert materials_action["cache_prefill"]["quels sont les 5 matériaux qui manquent le plus"] == {"count": 5}
    step_materials_action = next(
        action for action in action_manager.actions
        if action["name"] == "ship_upgrade_step_materials"
    )
    assert "ai-je les matériaux pour la prochaine amélioration" in step_materials_action["cache_prefill"]
    overview = next(action for action in action_manager.actions if action["name"] == "ship_upgrade_remaining_upgrades")
    assert overview["cache_prefill"]["que me reste-t-il à améliorer sur mon vaisseau"] == {}
    assert overview["cache_prefill"]["quels sont les autres modules à améliorer"] == {}
    assert overview["cache_prefill"]["what remains to improve on my ship"] == {}
    assert overview["cache_prefill"]["donne-moi la liste complète des améliorations restantes"] == {"list_all": True}
    assert overview["cache_prefill"]["list all remaining ship upgrades"] == {"list_all": True}
    assert "at most three priority modules" in overview["description"]
    slots = next(action for action in action_manager.actions if action["name"] == "ship_upgrade_weapon_slots")
    assert slots["cache_prefill"]["quelles armes ne sont pas à l'emplacement prévu"] == {}
    assert "actual and expected slot" in slots["description"]

    next_step_action = next(
        action for action in action_manager.actions
        if action["name"] == "ship_upgrade_next_step"
    )
    assert "experimental effect" in next_step_action["description"].lower()
    next_step_prefills = next_step_action["cache_prefill"]
    assert "j'ai un plan actif, quel effet expérimental dois-je appliquer" in next_step_prefills
    assert "what experimental effect should i apply" in next_step_prefills
    assert "programme l'amélioration suivante" in next_step_prefills

    advice_action = next(
        action for action in action_manager.actions
        if action["name"] == "ship_upgrade_build_advice"
    )
    prefills = advice_action["cache_prefill"]
    assert "prepare a complete route for all modules" in prefills
    assert "i want a full route for all modules" in prefills
    assert "prepare a route to improve my ship" in prefills
    assert "make me a complete route to improve my entire ship" in prefills
    assert "prépare une route pour améliorer mon vaisseau" in prefills
    navigation_action = next(
        action for action in action_manager.actions
        if action["name"] == "ship_upgrade_prepare_next_jump"
    )
    assert "programme-moi une route complète pour l'amélioration de mon vaisseau" in navigation_action["cache_prefill"]
    assert "programme-moi une route complète pour l'amélioration de mon vaisseau" not in prefills
    assert "prépare une route complète pour améliorer l'intégralité de mon vaisseau" in prefills
    assert "prepare a complete route to improve the entire ship" in prefills
    assert "je veux une route complète pour tous les modules" in prefills
    assert prefills["prépare une route complète pour l'amélioration de mon vaisseau"] == {}
    assert prefills["recalcule la route d'amélioration de mon vaisseau"] == {}
    assert prefills["prépare-moi une route d'amélioration intégrale pour mon vaisseau"] == {}
    assert advice_action["action_type"] == "global"
    assert "showUI" in advice_action["description"]
    assert "publishes" in advice_action["description"]
    assert advice_action["input_template"]({}, {}) == "Prepare complete ship-upgrade route"

    status_action = next(
        action for action in action_manager.actions
        if action["name"] == "ship_upgrade_plan_status"
    )
    status_prefills = status_action["cache_prefill"]
    assert "est-ce qu un plan d amelioration est charge" in status_prefills
    assert "quel plan est en cours" in status_prefills
    assert "mon plan d amelioration est il actif" in status_prefills

    loadout_action = next(
        action for action in action_manager.actions
        if action["name"] == "ship_upgrade_current_loadout"
    )
    loadout_prefills = loadout_action["cache_prefill"]
    assert loadout_prefills["quelle armure est installée"] == {"slot": "Armour"}
    assert loadout_prefills["what armor module is installed"] == {"slot": "Armour"}
    assert loadout_prefills["quel est le module d'armure que j'ai déjà"] == {"slot": "Armour"}
    assert loadout_prefills["et quel est le module d'armure que j'ai déjà"] == {"slot": "Armour"}

    assert prompt_generator.status_generators[0]({}) == []
    plugin._announce_upgrade('Completed')
    notice = event_manager.incoming.get_nowait()
    assert notice.plugin_event_name == 'registration-test-guid.upgrade_notice'
    assert 'Completed' in prompt_generator.event_handlers[0](notice)
    assert helper._namespace is None
    exported = manager.plugin_settings_configs['registration-test-guid']
    assert exported['key'] == 'registration-test-guid'
    assert exported['label'] == 'plugin.registration-test-guid.label'
    assert plugin.get_ui_settings()['label'] == '@label'
    assert manager.plugin_ui['registration-test-guid']['translations']['fr']['label']
    plugin.on_chat_stop(helper)


@pytest.mark.parametrize("phrase", [
    "prépare le saut pour la prochaine étape d'amélioration de mon vaisseau",
    "Euh, est-ce que tu peux me programmer la route pour le prochain ingénieur ?",
    "programme-moi une route complète pour l'amélioration de mon vaisseau",
])
def test_jump_request_prediction_calculates_then_prepares_and_resumes_without_recalculating(
    tmp_path: Path, monkeypatch, phrase: str,
) -> None:
    import json
    import plugins.ship_upgrade_manager.ship_upgrade_manager as sum_module
    from lib.projections.nav_info import NavInfoStateModel
    from lib.projections.current_status import CurrentStatusState

    journal_dir = tmp_path / "journals"
    journal_dir.mkdir()
    monkeypatch.setattr(sum_module, "get_ed_journals_path", lambda _config: str(journal_dir))
    monkeypatch.setattr(PluginHelper, "PLUGIN_DATA_PATH", str(tmp_path / "plugin-data"))
    cache = {}
    store = Mock()
    store.get.side_effect = cache.get
    store.set.side_effect = cache.__setitem__
    monkeypatch.setattr("lib.ActionManager.KeyValueStore", lambda _name: store)
    monkeypatch.setattr(ActionManager, "actions", {})
    action_manager = ActionManager()
    permissions = {name: True for name in (
        "plotToTarget", "galaxyMapOpenOrClose", "target_next_system_in_route",
    )}
    action_manager.set_allowed_actions(permissions)
    config = {"plugin_settings": {}, "game_actions_var": True, "allowed_actions": permissions}
    manager = PluginManager(config)
    states = {
        "Location": {"StarSystem": "Muth", "StarPos": [0, 0, 0]},
        "CurrentStatus": {"flags": {"InMainShip": True}, "flags2": {}, "GuiFocus": "NoFocus"},
        "NavInfo": {"NavRoute": [], "NextJumpTarget": None},
        "Loadout": {"ShipID": 1, "Ship": "python", "Modules": [
            {"Slot": "FrameShiftDrive", "Item": "int_hyperdrive_size4_class5"},
        ]},
    }
    event_manager = _EventManager()
    event_manager.get_current_state = lambda: ([], states)

    def wait(name, condition, timeout):
        model = (NavInfoStateModel if name == "NavInfo" else CurrentStatusState)(**states[name])
        if not condition(model):
            raise TimeoutError
        return model

    event_manager.wait_for_condition = wait
    calls = []

    def plot(args, _states):
        calls.append(("plot", args["system"]))
        states["NavInfo"] = {
            "NavRoute": [{"StarSystem": args["system"], "Scoopable": True}],
            "NextJumpTarget": args["system"],
        }
        states["CurrentStatus"]["GuiFocus"] = "GalaxyMap"
        return "Route plotted"

    def close(_args, _states):
        calls.append(("close", None))
        states["CurrentStatus"]["GuiFocus"] = "NoFocus"
        return "Galaxy map closed"

    def target(_args, _states):
        calls.append(("target", None))
        return "Targeting next system"

    for name, method, kind in (
        ("plotToTarget", plot, "ship"), ("galaxyMapOpenOrClose", close, "ship"),
        ("target_next_system_in_route", target, "mainship"),
    ):
        action_manager.registerAction(name, name, {}, method, kind, permission=name)
    helper = _PluginDataHelper(
        manager, _PromptGenerator(), config, action_manager, event_manager,
        None, None, None, None, _Assistant(), data_path=tmp_path,
    )
    plugin = ShipUpgradeManagerPlugin(
        PluginManifest('{"guid":"jump-test-guid","name":"Ship Upgrade","version":"1.0.0"}')
    )
    manager.plugin_list[plugin.plugin_manifest.guid] = plugin
    manager.register_settings()
    manager.on_chat_start(helper)
    plugin.import_plan("python", "Jump build", {"steps": [
        {"id": "fsd", "slot": "FrameShiftDrive", "item": "int_hyperdrive_size4_class5",
         "engineering": {"BlueprintName": "FSD_LongRange", "Level": 3}},
    ]})
    plugin._handle_ship_event({"event": "LoadGame", "FID": "F1", "ShipID": 1, "Ship": "python"})
    tools = action_manager.getToolsList("mainship", True, False, False, permissions)
    predicted = action_manager.predict_action(phrase, tools)
    assert predicted and predicted[0].function.name == "ship_upgrade_prepare_next_jump"

    reply = json.loads(action_manager.runAction(predicted[0], states)["content"])

    assert reply["status"] == "ready" and reply["calculated"] is True
    assert calls == [("plot", reply["system"]), ("close", None), ("target", None)]
    monkeypatch.setattr(plugin, "_build_advice_action", lambda *_: pytest.fail("Unexpected recalculation"))
    states["Location"] = {"StarSystem": "Intermediate", "StarPos": [5, 0, 0]}
    resumed = json.loads(action_manager.runAction(predicted[0], states)["content"])
    assert resumed["status"] == "ready" and resumed["calculated"] is False
    assert resumed["system"] == reply["system"]
    assert calls[-1] == ("target", None)
    plugin.on_chat_stop(helper)


@pytest.mark.parametrize("mode", [None, "mainship", "humanoid", "buggy", "fighter", "nomad"])
def test_route_request_is_available_predicted_executed_and_published_in_every_mode(
    tmp_path: Path, monkeypatch, mode: str | None,
) -> None:
    import plugins.ship_upgrade_manager.ship_upgrade_manager as sum_module

    journal_dir = tmp_path / "journals"
    journal_dir.mkdir()
    monkeypatch.setattr(sum_module, "get_ed_journals_path", lambda _config: str(journal_dir))
    monkeypatch.setattr(PluginHelper, "PLUGIN_DATA_PATH", str(tmp_path / "plugin-data"))
    cache = {}
    store = Mock()
    store.get.side_effect = cache.get
    store.set.side_effect = cache.__setitem__
    monkeypatch.setattr("lib.ActionManager.KeyValueStore", lambda _name: store)
    monkeypatch.setattr(ActionManager, "actions", {})
    action_manager = ActionManager()
    manager = PluginManager({"plugin_settings": {}})
    helper = _PluginDataHelper(
        manager, _PromptGenerator(), {}, action_manager, _EventManager(),
        None, None, None, None, _Assistant(), data_path=tmp_path,
    )
    plugin = ShipUpgradeManagerPlugin(
        PluginManifest('{"guid":"route-test-guid","name":"Ship Upgrade","version":"1.0.0"}')
    )
    manager.plugin_list[plugin.plugin_manifest.guid] = plugin
    manager.register_settings()
    manager.on_chat_start(helper)
    plugin.import_plan("python", "Route build", {"steps": [
        {"id": "fsd", "slot": "FrameShiftDrive", "item": "int_hyperdrive_size4_class5",
         "engineering": {"BlueprintName": "FSD_LongRange", "Level": 3}},
    ]})
    plugin._handle_ship_event({"event": "LoadGame", "FID": "F1", "ShipID": 1, "Ship": "python"})
    plugin._handle_ship_event({"event": "Loadout", "ShipID": 1, "Ship": "python", "Modules": [
        {"Slot": "FrameShiftDrive", "Item": "int_hyperdrive_size4_class5"},
    ]})

    tools = action_manager.getToolsList(mode, True, True, True, {})
    assert "ship_upgrade_build_advice" in {tool["function"]["name"] for tool in tools}
    disabled = action_manager.getToolsList(mode, False, True, True, {})
    assert "ship_upgrade_build_advice" not in {tool["function"]["name"] for tool in disabled}
    calls = action_manager.predict_action(
        "Prépare-moi une route d'amélioration intégrale pour mon vaisseau.", tools,
    )
    assert calls is not None and len(calls) == 1
    assert calls[0].function.name == "ship_upgrade_build_advice"
    result = action_manager.runAction(calls[0], {})
    assert "Route ready to upgrade 1 remaining module" in result["content"]
    with plugin.get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM upgrade_routes").fetchone()[0] == 1
    settings = manager.plugin_settings_configs[plugin.plugin_manifest.guid]
    route = next(grid for grid in settings["grids"] if grid["key"] == "route")
    content = str(route["fields"])
    assert "plugin.route-test-guid.route.summary" in content
    assert "plugin.route-test-guid.route.empty" not in content
    assert all(field["type"] != "button" for field in route["fields"])
    slot_calls = action_manager.predict_action(
        "Quelles armes ne sont pas à l'emplacement prévu ?", tools,
    )
    assert slot_calls is not None and len(slot_calls) == 1
    assert slot_calls[0].function.name == "ship_upgrade_weapon_slots"
    slot_result = __import__("json").loads(action_manager.runAction(slot_calls[0], {})["content"])
    assert slot_result["status"] == "ok"
    assert slot_result["mismatch_count"] == 0
    assert slot_result["weapons"] == []
    plugin.on_chat_stop(helper)


def test_registered_sideeffect_completes_module_from_game_event(tmp_path: Path, monkeypatch):
    action_manager = _ActionManager()
    event_manager = _EventManager()
    helper = _PluginDataHelper(
        PluginManager({"plugin_settings": {}}),
        _PromptGenerator(),
        {},
        action_manager,
        event_manager,
        None,
        None,
        None,
        None,
        _Assistant(),
        data_path=tmp_path,
    )
    plugin = ShipUpgradeManagerPlugin(
        PluginManifest(
            '{"guid":"event-test-guid","name":"Ship Upgrade","version":"1.0.0"}'
        )
    )
    manager = helper._plugin_manager
    monkeypatch.setattr(PluginHelper, 'PLUGIN_DATA_PATH', str(tmp_path / 'plugin-data'))
    manager.plugin_list[plugin.plugin_manifest.guid] = plugin
    manager.register_settings()
    manager.on_chat_start(helper)
    plugin.import_plan(
        "Python",
        "PvE",
        {
            "steps": [
                {
                    "id": "fsd",
                    "item": "int_hyperdrive_size5_class5",
                    "slot": "FrameShiftDrive",
                }
            ],
        },
    )
    plugin.start_session("PvE", "SHIP-1")

    event_manager.sideeffects[0](
        GameEvent(
            content={
                "event": "ModuleBuy",
                "ShipID": "SHIP-1",
                "BuyItem": "int_hyperdrive_size5_class5",
                "Slot": "FrameShiftDrive",
            },
            historic=False,
        ),
        {},
    )

    assert plugin.get_session()["completed_steps"] == ["fsd"]
