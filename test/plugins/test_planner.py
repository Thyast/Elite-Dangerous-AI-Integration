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
    StationSearchRun,
    _module_filter_key,
    _station_module_filter,
    build_advice,
    module_family,
    render_chain,
    select_station_route,
    select_station_route_from_search,
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


def test_engineering_increments_follow_grade_level_and_quality():
    """Grade N takes N increments (journal Quality +1/N each); an incomplete
    current grade is finished first."""
    from plugins.ship_upgrade_manager.planner import engineering_increments

    target = [{"slot": "Armour", "item": "reactive_armour",
               "engineering": {"BlueprintName": "Armour_Thermic", "Level": 5}}]

    def increments(level, quality):
        loadout = _loadout([{
            "Slot": "Armour", "Item": "smallcombat01_nx_armour_reactive",
            "Engineering": {"BlueprintName": "Armour_Thermic", "Level": level, "Quality": quality},
        }])
        return engineering_increments(analyze_build(target, loadout)[0])

    assert increments(2, 1.0) == [(3, 3), (4, 4), (5, 5)]
    assert increments(2, 0.5) == [(2, 1), (3, 3), (4, 4), (5, 5)]
    assert increments(3, 0.3329) == [(3, 2), (4, 4), (5, 5)]
    missing = analyze_build(target, _loadout([]))[0]
    assert engineering_increments(missing) == [(1, 1), (2, 2), (3, 3), (4, 4), (5, 5)]


def test_target_grade_counts_only_when_complete():
    """Max effect needs the grade completed: G5 at Quality 0.8 still needs
    one grade-5 increment; Quality 1.0 (or unknown) is installed."""
    from plugins.ship_upgrade_manager.planner import engineering_increments, grade_reached

    target = [{"slot": "Armour", "item": "reactive_armour",
               "engineering": {"BlueprintName": "Armour_Thermic", "Level": 5}}]

    def delta(engineering):
        loadout = _loadout([{"Slot": "Armour", "Item": "smallcombat01_nx_armour_reactive",
                             "Engineering": {"BlueprintName": "Armour_Thermic", **engineering}}])
        return analyze_build(target, loadout)[0]

    partial = delta({"Level": 5, "Quality": 0.8})
    assert partial.status == "grade_behind"
    assert engineering_increments(partial) == [(5, 1)]
    assert delta({"Level": 5, "Quality": 1.0}).status == "installed"
    assert delta({"Level": 5}).status == "installed"
    assert grade_reached(5, 0.2, 4) is True
    assert grade_reached(4, 1.0, 5) is False


def test_analyze_build_flags_missing_module():
    target = [{"slot": "Slot01_Size5", "item": "Int_ShieldGenerator_Size5_Class3_Fast",
               "engineering": {"BlueprintName": "ShieldGenerator_Thermic", "Level": 5}}]
    loadout = _loadout([])
    deltas = analyze_build(target, loadout)
    assert deltas[0].status == "missing"
    assert deltas[0].grades_needed == [1, 2, 3, 4, 5]


def test_reactive_armour_plan_matches_installed_reactive_surface_composite(catalog, engineers):
    target = [{
        "slot": "Armour",
        "item": "reactive_armour",
        "engineering": {"BlueprintName": "Armour_Thermic", "Level": 5},
    }]
    loadout = _loadout([{
        "Slot": "Armour",
        "Item": "smallcombat01_nx_armour_reactive",
        "Engineering": {"BlueprintName": "Armour_Thermic", "Level": 5},
    }])

    deltas = analyze_build(target, loadout)
    assert deltas[0].status == "installed"
    assert build_advice(deltas, catalog, engineers) == []


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


def test_station_route_grouped_then_fallback_only_for_uncovered_modules():
    requests = []
    filters = [
        {"name": "Beam Laser", "class": ["3"], "rating": ["A"]},
        {"name": "Power Plant", "class": ["5"], "rating": ["A"]},
        {"name": "Shield Generator", "class": ["5"], "rating": ["C"]},
    ]
    grouped_station = {
        "id": "sol::alpha", "name": "Alpha Station", "system": "Sol",
        "distance_ly": 5.0, "stock_status": "unknown", "modules": [],
        "matched_filters": [filters[0], filters[1]],
    }
    fallback_station = {
        "id": "lave::beta", "name": "Beta Station", "system": "Lave",
        "distance_ly": 8.0, "stock_status": "unknown", "modules": [],
        "matched_filters": [filters[2]],
    }

    def search(query, *, cache_key=None):
        requests.append((query, cache_key))
        if cache_key == "grouped":
            return {"status": "ok", "stations": [grouped_station]}
        return {"status": "ok", "stations": [fallback_station]}

    route, uncovered = select_station_route_from_search(filters, StationSearchRun(search))

    assert len(requests) == 2
    assert requests[0][1] == "grouped"
    assert requests[1][0] == [filters[2]]
    assert requests[1][1].startswith("module:")
    assert not uncovered
    assert [stop.station for stop in route] == ["Alpha Station", "Beta Station"]
    assert route[0].modules == ["Beam Laser", "Power Plant"]
    assert all(stop.stock_status == "unknown" for stop in route)


def test_station_search_run_deduplicates_identical_requests():
    calls = []
    result = {"status": "ok", "stations": []}

    def search(filters, **kwargs):
        calls.append((filters, kwargs))
        return result

    run = StationSearchRun(search)
    module = {"name": "Beam Laser", "class": ["3"], "rating": ["A"]}
    assert run([module], cache_key="grouped") is result
    assert run([dict(module)], cache_key="grouped") is result
    assert len(calls) == 1
    run([module], cache_key="fallback:beam")
    assert len(calls) == 2


def test_station_search_deduplicates_duplicate_module_filters():
    filters = [
        {"name": "Beam Laser", "class": ["3"], "rating": ["A"]},
        {"name": "beam laser", "class": ["3"], "rating": ["A"]},
    ]
    station = {
        "id": "sol::alpha", "name": "Alpha", "system": "Sol", "distance_ly": 2,
        "modules": [], "matched_filters": [filters[0]],
    }
    calls = []

    def search(query, *, cache_key=None):
        calls.append((query, cache_key))
        return {"status": "ok", "stations": [station]}

    route, uncovered = select_station_route_from_search(filters, StationSearchRun(search))
    assert len(calls) == 1
    assert len(calls[0][0]) == 1
    assert len(route) == 1
    assert not uncovered


def test_station_route_greedily_selects_station_covering_most_modules():
    filters = [
        {"name": "Beam Laser", "class": ["3"], "rating": ["A"]},
        {"name": "Power Plant", "class": ["5"], "rating": ["A"]},
        {"name": "Shield Generator", "class": ["5"], "rating": ["C"]},
    ]
    stations = [
        {"id": "sol::a", "name": "A", "system": "Sol", "distance_ly": 3,
         "modules": [], "matched_filters": filters[:2]},
        {"id": "lave::b", "name": "B", "system": "Lave", "distance_ly": 1,
         "modules": [], "matched_filters": [filters[2]]},
        {"id": "sol::c", "name": "C", "system": "Sol", "distance_ly": 1,
         "modules": [], "matched_filters": [filters[0]]},
    ]
    route, uncovered = select_station_route(filters, stations)
    assert [stop.station for stop in route] == ["A", "B"]
    assert not uncovered


def test_station_module_mapping_uses_exact_host_allowlist_names():
    assert _station_module_filter(
        "int_powerplant_size5_class5", lambda item: {"name": "Power Plant"}
    ) == {"name": "Power Plant", "class": ["5"], "rating": ["A"]}
    assert _station_module_filter(
        "hpt_beamlaser_gimbal_large", lambda item: {"name": "Beam Laser"}
    ) == {"name": "Beam Laser", "class": ["3"]}
    assert _station_module_filter(
        "some_unknown_module", lambda item: None
    ) is None


def test_build_advice_emits_one_station_purchase_for_shared_stop(catalog, engineers):
    deltas = analyze_build(
        [
            {"slot": "largehardpoint1", "item": "hpt_beamlaser_fixed_large"},
            {"slot": "powerplant", "item": "int_powerplant_size5_class5"},
        ],
        [],
    )
    filters = {
        "beam laser": {"name": "Beam Laser", "class": ["3"]},
        "power plant": {"name": "Power Plant", "class": ["5"], "rating": ["A"]},
    }
    station = {
        "id": "sol::jameson", "name": "Jameson Memorial", "system": "Sol",
        "distance_ly": 10, "stock_status": "unknown", "modules": [],
        "matched_filters": list(filters.values()),
    }

    def search(query, *, cache_key=None):
        return {"status": "ok", "stations": [station]}

    actions = build_advice(
        deltas, catalog, engineers, station_search=search,
        module_filter=lambda item: filters.get("beam laser" if "beamlaser" in item else "power plant"),
    )
    purchases = [action for action in actions if action.kind == "station_purchase"]
    assert len(purchases) == 1
    assert purchases[0].station == "Jameson Memorial"
    assert set(purchases[0].target.split(", ")) == {"Beam Laser", "Power Plant"}
    assert purchases[0].stock_status == "unknown"


def test_station_route_merges_station_duplicates_and_covers_two_modules():
    station1 = {
        "id": "sol::alpha", "name": "Alpha", "system": "Sol", "distance_ly": 4,
        "modules": [{"name": "Beam Laser", "class": "3", "rating": "A"}],
        "matched_filters": [{"name": "Beam Laser", "class": ["3"], "rating": ["A"]}],
    }
    station2 = {
        "id": "sol::alpha", "name": "Alpha", "system": "Sol", "distance_ly": 4,
        "modules": [{"name": "Power Plant", "class": "5", "rating": "A"}],
        "matched_filters": [{"name": "Power Plant", "class": ["5"], "rating": ["A"]}],
    }

    def search(filters, **_kwargs):
        if len(filters) > 1:
            return {"status": "ok", "stations": [station1]}
        return {"status": "ok", "stations": [station2]}

    route, uncovered = select_station_route(
        [
            {"name": "Beam Laser", "class": ["3"], "rating": ["A"]},
            {"name": "Power Plant", "class": ["5"], "rating": ["A"]},
        ],
        search,
    )

    assert len(route) == 1
    assert route[0].station == "Alpha"
    assert set(route[0].modules) == {"Beam Laser", "Power Plant"}
    assert not uncovered


def test_helper_station_search_per_calculation_cache(monkeypatch):
    import json
    import sys
    from pathlib import Path
    from types import SimpleNamespace

    sys.path.insert(0, str(Path(__file__).parents[2] / "src"))
    from lib.PluginHelper import PluginHelper

    calls = []
    monkeypatch.setattr(
        "lib.actions.actions_web.search_stations_for_modules",
        lambda modules, states, **kwargs: calls.append((modules, states, kwargs)) or {"status": "ok", "stations": []},
    )
    helper = PluginHelper.__new__(PluginHelper)
    helper._event_manager = SimpleNamespace(
        get_current_state=lambda: ([], {"Location": SimpleNamespace(model_dump=lambda: {"StarSystem": "Sol", "StarPos": [0, 0, 0]})})
    )
    cache = {}
    query = [{"name": "Beam Laser", "class": ["3"], "rating": ["A"]}]
    assert helper.search_stations_for_modules(query, cache=cache, cache_key="grouped")["status"] == "ok"
    helper.search_stations_for_modules(query, cache=cache, cache_key="grouped")
    assert len(calls) == 1
    helper.search_stations_for_modules(query, cache=cache, cache_key="single:beam")
    assert len(calls) == 2


def test_station_module_filter_uses_host_allowlist_exact_names():
    sys.path.insert(0, str(REPO / "src"))
    from lib.actions.data import known_modules

    expected = {
        "reactive_armour": "Reactive Surface Composite",
        "smallcombat01_nx_armour_reactive": "Reactive Surface Composite",
        "int_powerplant_size5_class5": "Power Plant",
        "hpt_beamlaser_gimbal_large": "Beam Laser",
        "hpt_shieldbooster_size0_class2": "Shield Booster",
        "hpt_heatsinklauncher_turret_tiny": "Heat Sink Launcher",
    }
    for item, name in expected.items():
        result = _station_module_filter(item)
        assert result is not None
        assert result["name"] == name
        assert result["name"] in known_modules
    assert _station_module_filter("int_unknown_custom_module_size2_class1") is None
    assert module_family("int_engine_size5_class5_agile") == "engine_agile"
    assert _station_module_filter("int_engine_size5_class5_agile") == {
        "name": "Mk II Agile Boost Thrusters", "class": ["5"], "rating": ["A"]
    }
    assert _station_module_filter("hpt_mkiiplasmashockautocannon_fixed_large") == {
        "name": "Mk II Plasma Shock Accelerator", "class": ["3"]
    }


def test_build_advice_uses_multi_module_station_route(catalog, engineers):
    target = [
        {"slot": "LargeHardpoint1", "item": "hpt_beamlaser_fixed_large"},
        {"slot": "PowerPlant", "item": "int_powerplant_size5_class5"},
    ]
    deltas = analyze_build(target, _loadout([]))
    search_calls = []
    requests = [
        {"name": "Beam Laser", "class": ["3"]},
        {"name": "Power Plant", "class": ["5"], "rating": ["A"]},
    ]
    stations = [{
        "id": "sol::jameson",
        "name": "Jameson Memorial",
        "system": "Shinrarta Dezhra",
        "distance_ly": 0.0,
        "stock_status": "unknown",
        "modules": [],
        "matched_filters": requests,
    }]

    def search(modules, *, cache_key=None, **_kwargs):
        search_calls.append((modules, cache_key))
        return {"status": "ok", "stations": stations}

    mapped = {
        "hpt_beamlaser_fixed_large": requests[0],
        "int_powerplant_size5_class5": requests[1],
    }
    actions = build_advice(
        deltas,
        catalog,
        engineers,
        station_search=search,
        module_filter=lambda item: mapped.get(item),
    )
    purchases = [action for action in actions if action.kind == "station_purchase"]
    assert len(purchases) == 1
    assert purchases[0].station == "Jameson Memorial"
    assert set(purchases[0].target.split(", ")) == {"Beam Laser", "Power Plant"}
    assert purchases[0].stock_status == "unknown"
    assert len(search_calls) == 1
    assert len(search_calls[0][0]) == 2
    assert actions.index(purchases[0]) == 0
    assert "First: station purchase" in render_chain(actions)


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
