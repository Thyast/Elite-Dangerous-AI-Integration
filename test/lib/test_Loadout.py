from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from lib.Event import GameEvent
from lib.projections.loadout import Loadout, LoadoutState


GUARDIAN = "int_guardianmodulereinforcement_size2_class1"
STANDARD = "int_modulereinforcement_size2_class2"
SMALL = "int_modulereinforcement_size1_class2"


def _process(projection, event, **payload):
    projection.process(GameEvent(content={
        "event": event, "timestamp": "2026-10-28T13:51:58Z", **payload,
    }, historic=False))
    LoadoutState.model_validate(projection.state.model_dump())


@pytest.fixture
def loadout():
    projection = Loadout()
    _process(projection, "Loadout", ShipID=15, Ship="smallcombat01_nx", Modules=[
        {"Slot": "Slot06_Size2", "Item": STANDARD},
        {"Slot": "Slot07_Size1", "Item": SMALL},
    ])
    return projection


@pytest.mark.parametrize("event,item_field", [
    ("ModuleRetrieve", "RetrievedItem"),
    ("ModuleBuy", "BuyItem"),
])
def test_fitting_replaces_only_the_named_slot_and_survives_serialization(loadout, event, item_field):
    _process(loadout, event, ShipID=15, Slot="Slot06_Size2",
             **{item_field: f"${GUARDIAN}_name;", "SwapOutItem": f"${STANDARD}_name;"})

    restored = LoadoutState.model_validate(loadout.state.model_dump())
    assert {module.Slot: module.Item for module in restored.Modules} == {
        "Slot06_Size2": GUARDIAN, "Slot07_Size1": SMALL,
    }


@pytest.mark.parametrize("event", ["ModuleStore", "ModuleSell"])
def test_removing_small_reinforcement_does_not_remove_other_slots(loadout, event):
    _process(loadout, event, ShipID=15, Slot="slot07_size1")
    assert [(module.Slot, module.Item) for module in loadout.state.Modules] == [
        ("Slot06_Size2", STANDARD),
    ]


def test_mass_storage_removes_only_listed_slots(loadout):
    _process(loadout, "MassModuleStore", ShipID=15, Items=[{"Slot": "Slot07_Size1"}])
    assert [module.Item for module in loadout.state.Modules] == [STANDARD]


def test_swap_moves_modules_without_losing_their_metadata(loadout):
    loadout.state.Modules[0].Health = 0.8
    _process(loadout, "ModuleSwap", ShipID=15,
             FromSlot="Slot06_Size2", ToSlot="Slot07_Size1")
    assert {module.Slot: module.Item for module in loadout.state.Modules} == {
        "Slot07_Size1": STANDARD, "Slot06_Size2": SMALL,
    }
    assert next(module for module in loadout.state.Modules if module.Item == STANDARD).Health == 0.8


def test_swap_into_empty_slot_does_not_create_a_null_module(loadout):
    _process(loadout, "ModuleSwap", ShipID=15,
             FromSlot="Slot06_Size2", ToSlot="Slot05_Size2", ToItem="Null")
    assert {module.Slot: module.Item for module in loadout.state.Modules} == {
        "Slot05_Size2": STANDARD, "Slot07_Size1": SMALL,
    }


@pytest.mark.parametrize("event", [
    "ModuleRetrieve", "ModuleBuy", "ModuleStore", "ModuleSell", "ModuleSwap", "MassModuleStore",
])
def test_outfitting_another_ship_does_not_change_this_loadout(loadout, event):
    before = loadout.state.model_dump()
    _process(loadout, event, ShipID=99, Slot="Slot06_Size2",
             FromSlot="Slot06_Size2", ToSlot="Slot07_Size1",
             RetrievedItem=GUARDIAN, BuyItem=GUARDIAN, Items=[{"Slot": "Slot06_Size2"}])
    assert loadout.state.model_dump() == before


@pytest.mark.parametrize("event", ["ModuleBuyAndStore", "ModuleSellRemote", "StoredModules"])
def test_storage_only_events_do_not_change_installed_modules(loadout, event):
    before = loadout.state.model_dump()
    _process(loadout, event, ShipID=15, Slot="Slot06_Size2",
             BuyItem=GUARDIAN, SellItem=STANDARD)
    assert loadout.state.model_dump() == before


def test_retrieved_engineering_keeps_reported_values_without_inventing_identifiers(loadout):
    _process(loadout, "ModuleRetrieve", ShipID=15, Slot="Slot06_Size2",
             RetrievedItem="$int_hullreinforcement_size2_class2_name;",
             EngineerModifications="HullReinforcement_HeavyDuty", Level=5, Quality=0.8)
    module = next(module for module in loadout.state.Modules if module.Slot == "Slot06_Size2")
    assert module.Engineering is not None
    assert module.Engineering.BlueprintName == "HullReinforcement_HeavyDuty"
    assert module.Engineering.Level == 5
    assert module.Engineering.Quality == 0.8
    assert module.Engineering.EngineerID is None
    assert module.Engineering.BlueprintID is None


def test_crafting_updates_only_the_matching_module(loadout):
    _process(loadout, "EngineerCraft", Slot="Slot06_Size2", Module=STANDARD,
             BlueprintName="Test_Blueprint", Level=4, Quality=0.5,
             ApplyExperimentalEffect="special_test")
    engineering = loadout.state.Modules[0].Engineering
    assert engineering is not None
    assert engineering.Level == 4
    assert engineering.ExperimentalEffect == "special_test"
    assert loadout.state.Modules[1].Engineering is None
    _process(loadout, "EngineerCraft", Slot="Slot06_Size2", Module=STANDARD,
             BlueprintName="Test_Blueprint", Level=5, Quality=1.0)
    assert loadout.state.Modules[0].Engineering.ExperimentalEffect == "special_test"
    before = loadout.state.model_dump()
    _process(loadout, "EngineerCraft", Slot="Slot06_Size2", Module=GUARDIAN,
             BlueprintName="Test_Blueprint", Level=1, Quality=0.0)
    assert loadout.state.model_dump() == before


def test_fresh_loadout_replaces_incremental_state(loadout):
    _process(loadout, "ModuleRetrieve", ShipID=15, Slot="Slot06_Size2", RetrievedItem=GUARDIAN)
    _process(loadout, "Loadout", ShipID=15, Modules=[])
    assert loadout.state.Modules == []
