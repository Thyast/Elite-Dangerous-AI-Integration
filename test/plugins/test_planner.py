"""Tests for the honest build planner (Specs/03 planning engine).

The planner compares the target build against the current loadout and
produces ordered, cited actions per the spec honesty rules (a missing or
unknown cost is flagged, never zero; engineer visits are grounded in the
local registry)."""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parents[2]
sys.path.insert(0, str(REPO))

from plugins.ship_upgrade_manager.planner import (  # noqa: E402
    analyze_build,
    build_advice,
    module_family,
)

CATALOG_PATH = REPO / "src" / "assets" / "module_catalog.json"
ENGINEERS_PATH = REPO / "src" / "assets" / "ship_engineers.json"


@pytest.fixture(scope="module")
def catalog() -> dict:
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def engineers() -> dict:
    return json.loads(ENGINEERS_PATH.read_text(encoding="utf-8"))


import json  # noqa: E402


def _loadout(modules: list[dict]) -> list[dict]:
    return [
        {**module, "Item": module["Item"], "Engineering": module.get("Engineering")}
        for module in modules
    ]


def test_analyze_build_detects_missing_and_grade_behind():
    target = [
        {"slot": "PowerPlant", "item": "Int_Powerplant_Size5_Class5",
         "engineering": {"BlueprintName": "PowerPlant_Armoured", "Level": 5}},
        {"slot": "FrameShiftDrive", "item": "Int_Hyperdrive_Overcharge_Size4_Class5",
         "engineering": {"BlueprintName": "FSD_LongRange", "Level": 5}},
    ]
    loadout = _loadout([
        {"Slot": "PowerPlant", "Item": "Int_Powerplant_Size5_Class5",
         "Engineering": {"BlueprintName": "PowerPlant_Armoured", "Level": 5}},
        {"Slot": "FrameShiftDrive", "Item": "Int_Hyperdrive_Overcharge_Size4_Class5",
         "Engineering": {"BlueprintName": "FSD_LongRange", "Level": 2}},
    ])
    deltas = analyze_build(target, loadout)
    by_slot = {delta.slot: delta for delta in deltas}
    assert by_slot["powerplant"].status == "installed"
    assert by_slot["frameshiftdrive"].status == "grade_behind"
    assert by_slot["frameshiftdrive"].grades_needed == [3, 4, 5]


def test_analyze_build_flags_missing_module():
    target = [{"slot": "Slot01_Size5", "item": "Int_ShieldGenerator_Size5_Class3_Fast",
               "engineering": {"BlueprintName": "ShieldGenerator_Thermic", "Level": 5}}]
    loadout = _loadout([])
    deltas = analyze_build(target, loadout)
    assert deltas[0].status == "missing"
    assert deltas[0].grades_needed == [1, 2, 3, 4, 5]


def test_plan_advise_proposes_engineer_visit_with_location(catalog, engineers):
    target = [
        {"slot": "Slot01_Size5", "item": "Int_ShieldGenerator_Size5_Class3_Fast",
         "engineering": {"BlueprintName": "ShieldGenerator_Thermic", "Level": 5}},
    ]
    loadout = _loadout([
        {"Slot": "Slot01_Size5", "Item": "Int_ShieldGenerator_Size5_Class3_Fast",
         "Engineering": {"BlueprintName": "ShieldGenerator_Thermic", "Level": 2}},
    ])
    deltas = analyze_build(target, loadout)
    actions = build_advice(deltas, catalog, engineers, current_position=(0, 0, 0))
    kinds = [action.kind for action in actions]
    assert "engineer_visit" in kinds
    assert "apply_blueprint" in kinds
    visit = next(action for action in actions if action.kind == "engineer_visit")
    assert visit.system, "engineer visit must be grounded in a system"
    assert visit.distance_ly is not None
    # grades ascending
    grades = [action.grade for action in actions if action.kind == "apply_blueprint"]
    assert grades == [3, 4, 5]


def test_plan_advise_flags_missing_module_first(catalog, engineers):
    target = [{"slot": "Slot01_Size5", "item": "Int_ShieldGenerator_Size5_Class3_Fast"}]
    loadout = _loadout([])
    deltas = analyze_build(target, loadout)
    actions = build_advice(deltas, catalog, engineers)
    assert actions[0].kind == "install_module"


def test_plan_advise_refuses_unknown_blueprint(catalog, engineers):
    target = [
        {"slot": "Slot01_Size5", "item": "Int_UnknownModule_Size2_Class1",
         "engineering": {"BlueprintName": "UnknownModule_Whatever", "Level": 3}},
    ]
    loadout = _loadout([])
    deltas = analyze_build(target, loadout)
    actions = build_advice(deltas, catalog, engineers)
    blocking = [action for action in actions if action.kind == "blocking"]
    assert blocking, "unknown blueprint must block with an explicit message"
    assert "not in the local catalog" in blocking[0].target


def test_plan_advise_unknown_material_is_flagged_not_zero(catalog, engineers):
    # Use a blueprint whose grade exists in the journal loadout but where one
    # cost resolves as unknown: simulate by monkeypatching the entry grades.
    target = [
        {"slot": "Slot01_Size5", "item": "Int_ShieldGenerator_Size5_Class3_Fast",
         "engineering": {"BlueprintName": "ShieldGenerator_Thermic", "Level": 5}},
    ]
    loadout = _loadout([
        {"Slot": "Slot01_Size5", "Item": "Int_ShieldGenerator_Size5_Class3_Fast"}
    ])
    deltas = analyze_build(target, loadout)
    catalog_copy = json.loads(json.dumps(catalog))
    entry = next(
        blueprint for blueprint in catalog_copy["engineering_blueprints"]
        if blueprint["id"] == "ShieldGenerator_Thermic"
    )
    entry["grades"]["4"]["material_costs"] = [
        {"material_id": "unobtanium", "material_localised": "Unobtanium",
         "quantity": 1, "cost_status": "unknown", "source_name": "Unobtanium"}
    ]
    actions = build_advice(deltas, catalog_copy, engineers)
    flagged = [
        action for action in actions
        if action.kind == "apply_blueprint" and action.cost_status == "unknown"
    ]
    assert flagged, "unknown materials must be flagged, never zero"
    assert any("unverified" in action.rationale or "verify" in action.rationale for action in flagged)


def test_engineer_visit_reuses_one_visit_per_engineer(catalog, engineers):
    target = [
        {"slot": "TinyHardpoint1", "item": "Hpt_ShieldBooster_Size0_Class2",
         "engineering": {"BlueprintName": "ShieldBooster_Resistive", "Level": 5}},
        {"slot": "TinyHardpoint2", "item": "Hpt_ShieldBooster_Size0_Class2",
         "engineering": {"BlueprintName": "ShieldBooster_Resistive", "Level": 5}},
    ]
    loadout = _loadout([
        {"Slot": "TinyHardpoint1", "Item": "Hpt_ShieldBooster_Size0_Class2"},
        {"Slot": "TinyHardpoint2", "Item": "Hpt_ShieldBooster_Size0_Class2"},
    ])
    deltas = analyze_build(target, loadout)
    actions = build_advice(deltas, catalog, engineers)
    visits = [action for action in actions if action.kind == "engineer_visit"
              and action.target == "Etienne Dorn"]
    assert len(visits) <= 1, "one visit per engineer, not per grade/slot"
