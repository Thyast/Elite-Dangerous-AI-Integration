import sys
from pathlib import Path
from queue import Queue

sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from lib.PluginHelper import PluginHelper
from lib.PluginManager import PluginManager
from lib.PluginBase import PluginManifest
from lib.Event import GameEvent
from plugins.ship_upgrade_manager.ship_upgrade_manager import (
    ShipUpgradeManagerPlugin,
    ShipUpgradeProjection,
)


class _ActionManager:
    def __init__(self):
        self.actions = []

    def registerAction(self, **kwargs):
        self.actions.append(kwargs)


class _EventManager:
    def __init__(self):
        self.projections = []
        self.sideeffects = []
        self.incoming = Queue()

    def register_projection(self, projection, raise_error=False):
        self.projections.append((projection, raise_error))

    def register_sideeffect(self, sideeffect):
        self.sideeffects.append(sideeffect)


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

    def get_plugin_data_path(self, _manifest):
        self.data_path.mkdir(parents=True, exist_ok=True)
        return str(self.data_path)


def test_ship_upgrade_plugin_registers_with_real_plugin_helper(tmp_path: Path):
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

    plugin.on_chat_start(helper)

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
        "ship_upgrade_workshop_upgrades",
        "ship_upgrade_list_plans",
        "ship_upgrade_preview_changes",
        "ship_upgrade_apply_changes",
        "ship_upgrade_build_advice",
        "ship_upgrade_plan_status",
        "ship_upgrade_current_loadout",
        "ship_upgrade_state",
        "ship_upgrade_goal",
        "ship_upgrade_missing_modules",
        "ship_upgrade_missing_materials",
        "ship_upgrade_material_shortfall",
        "ship_upgrade_step_materials",
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
    assert "programme-moi une route complète pour l'amélioration de mon vaisseau" in prefills
    assert "prépare une route complète pour améliorer l'intégralité de mon vaisseau" in prefills
    assert "prepare a complete route to improve the entire ship" in prefills
    assert "je veux une route complète pour tous les modules" in prefills
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
    plugin.on_chat_stop(helper)


def test_registered_sideeffect_completes_module_from_game_event(tmp_path: Path):
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
    plugin.on_chat_start(helper)
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
