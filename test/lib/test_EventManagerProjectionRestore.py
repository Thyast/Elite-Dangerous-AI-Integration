from pathlib import Path
import sys
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from lib.Event import GameEvent
from lib.EventManager import EventManager
from lib.projections.loadout import Loadout


ITEM = "hpt_slugshot_gimbal_small"


def _baseline():
    return GameEvent(content={
        "event": "Loadout", "ShipID": 15, "Modules": [
            {"Slot": f"SmallHardpoint{i}", "Item": ITEM} for i in (1, 2)
        ],
    }, historic=True)


def _craft(slot, processed_at):
    return GameEvent(content={
        "event": "EngineerCraft", "Slot": slot, "Module": ITEM,
        "BlueprintName": "Weapon_HighCapacity", "Level": 3, "Quality": 1.0,
        "ExperimentalEffect": "special_corrosive_shell",
    }, processed_at=processed_at, historic=True)


def _manager(snapshot, events):
    manager = EventManager.__new__(EventManager)
    manager.projections = []
    manager.processed = events
    manager.pending = []
    manager.projection_store = Mock()
    manager.projection_store.init.return_value = snapshot
    manager.check_conditions = Mock()
    return manager


@pytest.mark.parametrize("fresh", [True, False])
def test_restore_never_replaces_completed_weapon_with_old_journal_baseline(fresh):
    initial = Loadout()
    baseline = _baseline()
    first = _craft("SmallHardpoint1", 10)
    second = _craft("SmallHardpoint2", 20)
    if not fresh:
        initial.process(baseline)
        initial.process(first)
    manager = _manager({
        "state": initial.state.model_dump(), "last_processed": 0 if fresh else 10,
    }, [baseline, first, second])

    projection = Loadout()
    manager.register_projection(projection)

    assert projection.last_processed == 20
    assert len(projection.state.Modules) == 2
    for module in projection.state.Modules:
        assert module.Engineering is not None
        assert module.Engineering.Level == 3
        assert module.Engineering.ExperimentalEffect == "special_corrosive_shell"
    saved = manager.projection_store.set.call_args.args[1]
    assert all(module["Engineering"]["Level"] == 3 for module in saved["state"]["Modules"])


def test_restored_projection_with_no_new_crafts_keeps_both_weapons():
    initial = Loadout()
    baseline = _baseline()
    crafts = [_craft(f"SmallHardpoint{i}", i * 10) for i in (1, 2)]
    initial.process(baseline)
    for event in crafts:
        initial.process(event)
    before = initial.state.model_dump()
    manager = _manager({"state": before, "last_processed": 20}, [baseline, *crafts])

    projection = Loadout()
    manager.register_projection(projection)

    assert projection.state.model_dump() == before
