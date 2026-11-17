"""Stop ordering for the Ship Upgrade Manager route.

Pure module — no COVAS:NEXT imports, fully testable. Stops (outfitting
stations, material traders, engineers) are ordered as an open path from the
commander's position, chaining straight-line distances from one stop to the
next, while honouring precedence constraints (buy a module before engineering
it, trade materials before the engineer that consumes them).

Small routes are solved exactly; larger ones use nearest neighbour followed by
or-opt / 2-opt local search. Stops without coordinates cannot be placed by
distance: they are inserted as late as their constraints allow.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

Coords = tuple[float, float, float]

# Up to this many located stops the order is exhaustive (8! = 40320 paths,
# pruned by precedence and by the best length found so far).
EXACT_LIMIT = 8
_EPSILON = 1e-9


def coords_of(value: Any) -> Coords | None:
    """(x, y, z) from a Spansh/registry dict, a journal StarPos list or a tuple."""
    if isinstance(value, dict):
        value = (value.get("x"), value.get("y"), value.get("z"))
    if not isinstance(value, (list, tuple)) or len(value) < 3:
        return None
    try:
        return (float(value[0]), float(value[1]), float(value[2]))
    except (TypeError, ValueError):
        return None


def distance_ly(first: Coords | None, second: Coords | None) -> float | None:
    if first is None or second is None:
        return None
    return sum((a - b) ** 2 for a, b in zip(first, second)) ** 0.5


@dataclass
class RouteStop:
    key: str
    coords: Coords | None
    after: set[str] = field(default_factory=set)  # keys that must be visited first
    payload: Any = None


def _leg(first: Coords | None, second: Coords | None) -> float:
    """Leg cost; an unknown end (no origin, no coordinates) costs nothing."""
    value = distance_ly(first, second)
    return 0.0 if value is None else value


def path_length(order: Sequence[RouteStop], origin: Coords | None) -> float:
    total = 0.0
    position = origin
    for stop in order:
        total += _leg(position, stop.coords)
        position = stop.coords if stop.coords is not None else position
    return total


def _predecessors(stops: Sequence[RouteStop]) -> dict[str, set[str]]:
    """Transitive predecessors, restricted to the stops of the route."""
    keys = {stop.key for stop in stops}
    direct = {stop.key: {key for key in stop.after if key in keys} for stop in stops}
    closure: dict[str, set[str]] = {}

    def visit(key: str, stack: tuple[str, ...]) -> set[str]:
        if key in closure:
            return closure[key]
        if key in stack:
            raise ValueError(f"route precedence cycle through {key}")
        result: set[str] = set()
        for before in direct[key]:
            result.add(before)
            result |= visit(before, stack + (key,))
        closure[key] = result
        return result

    for stop in stops:
        visit(stop.key, ())
    return closure


def _valid(order: Sequence[RouteStop], preds: dict[str, set[str]]) -> bool:
    seen: set[str] = set()
    for stop in order:
        if not preds.get(stop.key, set()) <= seen:
            return False
        seen.add(stop.key)
    return True


def _exact(stops: list[RouteStop], origin: Coords | None, preds: dict[str, set[str]]) -> list[RouteStop]:
    ordered = sorted(stops, key=lambda stop: stop.key)
    best_length = float("inf")
    best: list[RouteStop] = []
    path: list[RouteStop] = []
    visited: set[str] = set()

    def search(position: Coords | None, length: float) -> None:
        nonlocal best_length, best
        if length >= best_length - _EPSILON:
            return
        if len(path) == len(ordered):
            best_length, best = length, list(path)
            return
        for stop in ordered:
            if stop.key in visited or not preds[stop.key] <= visited:
                continue
            visited.add(stop.key)
            path.append(stop)
            search(stop.coords, length + _leg(position, stop.coords))
            path.pop()
            visited.discard(stop.key)

    search(origin, 0.0)
    return best


def _nearest_neighbour(stops: list[RouteStop], origin: Coords | None, preds: dict[str, set[str]]) -> list[RouteStop]:
    remaining = sorted(stops, key=lambda stop: stop.key)
    order: list[RouteStop] = []
    visited: set[str] = set()
    position = origin
    while remaining:
        ready = [stop for stop in remaining if preds[stop.key] <= visited]
        chosen = min(ready, key=lambda stop: (_leg(position, stop.coords), stop.key))
        order.append(chosen)
        visited.add(chosen.key)
        remaining.remove(chosen)
        position = chosen.coords
    return order


def _local_search(order: list[RouteStop], origin: Coords | None, preds: dict[str, set[str]]) -> list[RouteStop]:
    best = list(order)
    best_length = path_length(best, origin)
    improved = True
    while improved:
        improved = False
        size = len(best)
        # or-opt: move one stop elsewhere.
        for source in range(size):
            for target in range(size):
                if source == target:
                    continue
                candidate = list(best)
                stop = candidate.pop(source)
                candidate.insert(target, stop)
                length = path_length(candidate, origin)
                if length < best_length - _EPSILON and _valid(candidate, preds):
                    best, best_length, improved = candidate, length, True
        # 2-opt: reverse a segment.
        for start in range(size - 1):
            for end in range(start + 1, size):
                candidate = best[:start] + best[start:end + 1][::-1] + best[end + 1:]
                length = path_length(candidate, origin)
                if length < best_length - _EPSILON and _valid(candidate, preds):
                    best, best_length, improved = candidate, length, True
    return best


def _insert_unlocated(order: list[RouteStop], unlocated: list[RouteStop], preds: dict[str, set[str]]) -> list[RouteStop]:
    """Place stops without coordinates as late as their constraints allow."""
    result = list(order)
    pending = sorted(unlocated, key=lambda stop: stop.key)
    while pending:
        pending_keys = {item.key for item in pending}
        stop = next(
            (item for item in pending if not preds[item.key] & pending_keys),
            pending[0],
        )
        pending.remove(stop)
        successors = [
            index for index, other in enumerate(result) if stop.key in preds.get(other.key, set())
        ]
        result.insert(min(successors) if successors else len(result), stop)
    return result


def order_stops(stops: Sequence[RouteStop], origin: Coords | None) -> list[RouteStop]:
    """Shortest open path found for the stops, respecting precedence."""
    stops = list(stops)
    if not stops:
        return []
    preds = _predecessors(stops)
    located = [stop for stop in stops if stop.coords is not None]
    unlocated = [stop for stop in stops if stop.coords is None]
    located_keys = {stop.key for stop in located}
    located_preds = {key: before & located_keys for key, before in preds.items()}
    if len(located) <= EXACT_LIMIT:
        order = _exact(located, origin, located_preds)
    else:
        order = _local_search(
            _nearest_neighbour(located, origin, located_preds), origin, located_preds
        )
    return _insert_unlocated(order, unlocated, preds)


def refine(order: Sequence[RouteStop], origin: Coords | None) -> list[RouteStop]:
    """The shorter of ``order`` (e.g. built by insertion) and a fresh ordering."""
    fresh = order_stops(order, origin)
    if path_length(fresh, origin) < path_length(order, origin) - _EPSILON:
        return fresh
    return list(order)


def best_insertion(
    order: Sequence[RouteStop],
    origin: Coords | None,
    candidates: Sequence[RouteStop],
    latest_index: int | None = None,
) -> tuple[RouteStop, int] | None:
    """Candidate and position adding the smallest detour to ``order``.

    ``latest_index`` bounds the position (the stop must come before the stop
    currently at that index)."""
    limit = len(order) if latest_index is None else max(0, min(latest_index, len(order)))
    best: tuple[float, str, int, RouteStop] | None = None
    for candidate in candidates:
        if candidate.coords is None:
            continue
        for index in range(limit + 1):
            previous = origin if index == 0 else order[index - 1].coords
            following = order[index].coords if index < len(order) else None
            detour = _leg(previous, candidate.coords) + _leg(candidate.coords, following)
            if following is not None:
                detour -= _leg(previous, following)
            key = (detour, candidate.key, index, candidate)
            if best is None or key[:3] < best[:3]:
                best = key
    if best is None:
        return None
    return best[3], best[2]
