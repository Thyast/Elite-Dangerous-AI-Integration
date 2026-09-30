"""Route optimisation of the build advice: engineer grouping and access,
chained stop order, purchase/trade precedence and trader stops."""
import sys
from pathlib import Path

REPO = Path(__file__).parents[2]
sys.path.insert(0, str(REPO))

from plugins.ship_upgrade_manager.planner import (  # noqa: E402
    PlannedTrade,
    SlotDelta,
    build_advice,
    render_chain,
)


def _grade(engineers, material="iron", quantity=1):
    return {
        "material_costs": [{"material_id": material, "quantity": quantity, "cost_status": "verified"}],
        "engineers": engineers,
    }


CATALOG = {
    "engineering_blueprints": [
        {"id": "Widget_Strong", "fdname": "Widget_Strong", "grades": {
            "1": _grade(["Alice", "Bob"]),
            "2": _grade(["Alice", "Bob"]),
            "3": _grade(["Bob"]),
        }},
        {"id": "Gadget_Fast", "fdname": "Gadget_Fast", "grades": {
            "1": _grade(["Bob", "Carol"], material="carbon"),
            "2": _grade(["Bob", "Carol"], material="carbon"),
        }},
        {"id": "Gizmo_Light", "fdname": "Gizmo_Light", "grades": {
            "1": _grade(["Carol"], material="carbon", quantity=5),
        }},
    ]
}


def _engineer(name, x):
    return {"Engineer": name, "Location": f"{name} System", "Workshop": f"{name} Base",
            "Coords": {"x": x, "y": 0, "z": 0}}


REGISTRY = {
    "1": _engineer("Alice", 5),
    "2": _engineer("Bob", 40),
    "3": _engineer("Carol", -20),
}


def _delta(slot, blueprint, grades, status="blueprint_different", item=None):
    return SlotDelta(
        slot=slot,
        target_item=item or f"int_{slot}_size1_class1",
        target_blueprint=blueprint,
        target_level=max(grades),
        current_item=None if status == "missing" else item or f"int_{slot}_size1_class1",
        current_blueprint=None,
        current_level=None,
        status=status,
        grades_needed=list(grades),
    )


def _visits(actions):
    return [action.target for action in actions if action.kind == "engineer_visit"]


def test_engineer_already_on_route_is_reused_not_duplicated():
    # Widget grades 1-3 only share Bob; a second widget must not add Alice.
    deltas = [_delta("a", "widget_strong", [1, 2, 3]), _delta("b", "widget_strong", [1, 2])]

    actions = build_advice(deltas, CATALOG, REGISTRY, current_position=(0, 0, 0))

    assert _visits(actions) == ["Bob"]
    applied = [(a.slot, a.grade) for a in actions if a.kind == "apply_blueprint"]
    assert applied == [("a", 1), ("a", 2), ("a", 3), ("b", 1), ("b", 2)]


def test_one_engineer_covering_several_blueprints_is_preferred():
    # Bob applies both blueprints: one visit instead of Alice + Carol.
    deltas = [_delta("a", "widget_strong", [1, 2]), _delta("b", "gadget_fast", [1, 2])]

    actions = build_advice(deltas, CATALOG, REGISTRY, current_position=(0, 0, 0))

    assert _visits(actions) == ["Bob"]


def test_unlocked_engineer_is_preferred_over_closer_locked_one():
    progress = {"Alice": {"Progress": "Invited"}, "Bob": {"Progress": "Unlocked"}}

    actions = build_advice(
        [_delta("a", "widget_strong", [1])], CATALOG, REGISTRY,
        current_position=(0, 0, 0), engineer_progress=progress,
    )

    [visit] = [a for a in actions if a.kind == "engineer_visit"]
    assert visit.target == "Bob"
    assert visit.access is None


def test_locked_engineer_is_a_flagged_fallback_when_none_is_unlocked():
    progress = {"Bob": {"Progress": "Unlocked"}}

    actions = build_advice(
        [_delta("c", "gizmo_light", [1])], CATALOG, REGISTRY,
        current_position=(0, 0, 0), engineer_progress=progress,
    )

    [visit] = [a for a in actions if a.kind == "engineer_visit"]
    assert visit.target == "Carol"
    assert visit.access == "Not discovered"
    assert "(not unlocked yet: Not discovered)" in render_chain(actions)


def test_barred_engineer_is_never_proposed():
    progress = {"Carol": {"Progress": "Barred"}}

    actions = build_advice(
        [_delta("c", "gizmo_light", [1])], CATALOG, REGISTRY,
        current_position=(0, 0, 0), engineer_progress=progress,
    )

    assert _visits(actions) == []
    [blocking] = [a for a in actions if a.kind == "blocking"]
    assert "barred" in blocking.target


def test_stops_are_chained_from_one_to_the_next():
    # Bob covers both widgets, Carol the gizmo. Sorted by distance from the
    # origin: Bob (40) then Carol: 40 + 60 = 100 ly. Chained: Carol then Bob:
    # 20 + 60 = 80 ly.
    deltas = [
        _delta("a", "widget_strong", [1, 2, 3]),
        _delta("c", "gizmo_light", [1]),
        _delta("d", "widget_strong", [1]),
    ]

    actions = build_advice(deltas, CATALOG, REGISTRY, current_position=(0, 0, 0))

    visits = [a for a in actions if a.kind == "engineer_visit"]
    assert [v.target for v in visits] == ["Carol", "Bob"]
    assert [v.leg_distance_ly for v in visits] == [20.0, 60.0]
    assert [v.distance_ly for v in visits] == [20.0, 40.0]
    chain = render_chain(actions)
    assert "(Carol System, 20.0 ly from you)" in chain
    assert "(Bob System, 60.0 ly further)" in chain
    assert "Total route: 80.0 ly." in chain


def test_module_is_bought_before_the_engineer_that_modifies_it():
    item = "int_powerplant_size1_class1"
    delta = _delta("c", "gizmo_light", [1], status="missing", item=item)
    request = {"name": "Power Plant", "class": ["1"], "rating": ["E"]}
    station = {
        "id": "far::outfit", "name": "Far Outfit", "system": "Far", "distance_ly": 100.0,
        "coords": {"x": 100, "y": 0, "z": 0}, "modules": [], "matched_filters": [request],
    }

    actions = build_advice(
        [delta], CATALOG, REGISTRY, current_position=(0, 0, 0),
        station_search=lambda filters, **_kwargs: {"status": "ok", "stations": [station]},
    )

    kinds = [a.kind for a in actions]
    assert kinds.index("station_purchase") < kinds.index("engineer_visit")
    visit = actions[kinds.index("engineer_visit")]
    assert visit.leg_distance_ly == 120.0


def test_trader_stop_precedes_dependent_engineer_and_covers_the_shortfall():
    trades = [PlannedTrade("Raw", "nickel", "Nickel", 2, "carbon", "Carbon", 6)]
    searched = []

    def trader_search(category):
        searched.append(category)
        return {"status": "ok", "stations": [
            {"id": "detour::t", "name": "Detour Trader", "system": "Detour",
             "distance_ly": 30.0, "coords": {"x": 0, "y": 30, "z": 0}},
            {"id": "path::t", "name": "Path Trader", "system": "Path",
             "distance_ly": 10.0, "coords": {"x": -10, "y": 0, "z": 0}},
        ]}

    actions = build_advice(
        [_delta("c", "gizmo_light", [1])], CATALOG, REGISTRY, current_position=(0, 0, 0),
        inventory={"nickel": 2}, trades=trades, trader_search=trader_search,
    )

    assert searched == ["Raw"]
    kinds = [a.kind for a in actions]
    assert kinds == ["material_trade", "engineer_visit", "apply_blueprint"]
    trade = actions[0]
    assert trade.station == "Path Trader"
    assert trade.leg_distance_ly == 10.0
    assert actions[2].stock_status == "sufficient"
    assert "trade 2 Nickel for 6 Carbon" in render_chain(actions)


def test_without_trader_stop_the_shortfall_stays_missing():
    actions = build_advice(
        [_delta("c", "gizmo_light", [1])], CATALOG, REGISTRY, current_position=(0, 0, 0),
        inventory={"nickel": 2},
    )

    assert [a.kind for a in actions] == ["engineer_visit", "apply_blueprint"]
    assert actions[1].stock_status == "missing"


def test_missing_trader_is_reported_as_blocking():
    trades = [PlannedTrade("Encoded", "a", "A", 6, "b", "B", 1)]

    actions = build_advice(
        [_delta("c", "gizmo_light", [1])], CATALOG, REGISTRY, current_position=(0, 0, 0),
        trades=trades, trader_search=lambda _category: {"status": "error", "stations": []},
    )

    assert actions[0].kind == "blocking"
    assert actions[0].target == "no encoded material trader found"
    assert "material_trade" not in [a.kind for a in actions]
