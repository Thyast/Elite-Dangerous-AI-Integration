"""Tests for the Ship Upgrade Manager stop ordering (route.py)."""
import itertools
import random
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parents[2]
sys.path.insert(0, str(REPO))

from plugins.ship_upgrade_manager import route  # noqa: E402
from plugins.ship_upgrade_manager.route import (  # noqa: E402
    RouteStop,
    best_insertion,
    coords_of,
    order_stops,
    path_length,
)


def _keys(order):
    return [stop.key for stop in order]


def _brute_force(stops, origin):
    preds = route._predecessors(stops)
    best = None
    for permutation in itertools.permutations(stops):
        if not route._valid(permutation, preds):
            continue
        length = path_length(permutation, origin)
        if best is None or length < best:
            best = length
    return best


def test_coords_of_accepts_registry_dicts_and_star_pos_lists():
    assert coords_of({"x": 1, "y": 2.5, "z": -3}) == (1.0, 2.5, -3.0)
    assert coords_of([4, 5, 6]) == (4.0, 5.0, 6.0)
    assert coords_of({"x": None, "y": 1, "z": 2}) is None
    assert coords_of(None) is None


def test_order_chains_distances_instead_of_distance_from_start():
    # Sorted by distance from the origin the order would be A, C, B (10 + 21
    # + 31 = 62 ly). Chaining gives C, A, B (11 + 21 + 10 = 42 ly).
    stops = [
        RouteStop("a", (10, 0, 0)),
        RouteStop("b", (20, 0, 0)),
        RouteStop("c", (-11, 0, 0)),
    ]

    order = order_stops(stops, (0, 0, 0))

    assert _keys(order) == ["c", "a", "b"]
    assert path_length(order, (0, 0, 0)) == pytest.approx(42)


def test_order_respects_precedence_constraints():
    stops = [
        RouteStop("engineer", (1, 0, 0), after={"station"}),
        RouteStop("station", (50, 0, 0)),
    ]

    assert _keys(order_stops(stops, (0, 0, 0))) == ["station", "engineer"]


def test_transitive_precedence_is_enforced():
    stops = [
        RouteStop("c", (1, 0, 0), after={"b"}),
        RouteStop("b", (2, 0, 0), after={"a"}),
        RouteStop("a", (30, 0, 0)),
    ]

    assert _keys(order_stops(stops, (0, 0, 0))) == ["a", "b", "c"]


def test_precedence_cycle_is_rejected():
    with pytest.raises(ValueError):
        order_stops([RouteStop("a", (0, 0, 0), after={"b"}), RouteStop("b", (1, 0, 0), after={"a"})], None)


def test_exact_order_is_optimal_for_small_routes():
    rng = random.Random(7)
    for _ in range(20):
        stops = [
            RouteStop(f"s{index}", tuple(rng.uniform(-100, 100) for _ in range(3)))
            for index in range(6)
        ]
        stops[3].after = {"s0"}
        stops[5].after = {"s3", "s1"}
        order = order_stops(stops, (0, 0, 0))
        assert route._valid(order, route._predecessors(stops))
        assert path_length(order, (0, 0, 0)) == pytest.approx(_brute_force(stops, (0, 0, 0)))


def test_heuristic_handles_large_routes_with_constraints():
    rng = random.Random(3)
    stops = [
        RouteStop(f"s{index:02d}", tuple(rng.uniform(-500, 500) for _ in range(3)))
        for index in range(20)
    ]
    for index in range(10, 20):
        stops[index].after = {f"s{index - 10:02d}"}

    order = order_stops(stops, (0, 0, 0))

    assert sorted(_keys(order)) == sorted(stop.key for stop in stops)
    assert route._valid(order, route._predecessors(stops))
    naive = sorted(stops, key=lambda stop: stop.key)
    assert path_length(order, (0, 0, 0)) < path_length(naive, (0, 0, 0))


def test_unlocated_stops_go_as_late_as_constraints_allow():
    stops = [
        RouteStop("far", (100, 0, 0)),
        RouteStop("near", (1, 0, 0)),
        RouteStop("unknown", None),
        RouteStop("engineer", (2, 0, 0), after={"unknown-station"}),
        RouteStop("unknown-station", None),
    ]

    order = order_stops(stops, (0, 0, 0))

    assert _keys(order) == ["near", "unknown-station", "engineer", "far", "unknown"]


def test_route_without_origin_does_not_count_the_first_leg():
    stops = [RouteStop("a", (100, 0, 0)), RouteStop("b", (101, 0, 0))]

    order = order_stops(stops, None)

    assert path_length(order, None) == pytest.approx(1)


def test_best_insertion_picks_smallest_detour_before_limit():
    order = [RouteStop("a", (10, 0, 0)), RouteStop("b", (20, 0, 0))]
    candidates = [RouteStop("trader-far", (15, 30, 0)), RouteStop("trader-near", (15, 1, 0))]

    stop, index = best_insertion(order, (0, 0, 0), candidates)
    assert (stop.key, index) == ("trader-near", 1)

    stop, index = best_insertion(order, (0, 0, 0), candidates, latest_index=0)
    assert (stop.key, index) == ("trader-near", 0)
