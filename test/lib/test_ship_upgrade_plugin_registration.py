import sys
from pathlib import Path

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

    def register_projection(self, projection, raise_error=False):
        self.projections.append((projection, raise_error))

    def register_sideeffect(self, sideeffect):
        self.sideeffects.append(sideeffect)


class _PromptGenerator:
    def __init__(self):
        self.status_generators = []

    def register_status_generator(self, generator):
        self.status_generators.append(generator)


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
        None,
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
    assert {
        action["name"] for action in action_manager.actions
    } == {
        "ship_upgrade_start_session",
        "ship_upgrade_complete_step",
        "ship_upgrade_pause_session",
        "ship_upgrade_resume_session",
        "ship_upgrade_stop_session",
        "ship_upgrade_next_step",
        "ship_upgrade_list_plans",
        "ship_upgrade_preview_changes",
        "ship_upgrade_apply_changes",
    }

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
        None,
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
