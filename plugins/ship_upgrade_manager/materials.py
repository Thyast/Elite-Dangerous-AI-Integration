"""Engineering material needs versus the commander's stock.

Pure module (no COVAS:NEXT imports): the plugin injects the reference data
(journal material template, display names, material family maps), the
blueprint catalog and the experimental-effect catalog.

Grade N of a blueprint takes N increments, each at the catalog recipe cost
(journal EngineerCraft Quality rises by 1/N per increment); an incomplete
current grade is finished first. The experimental effect is applied once.
Unknown costs are reported, never treated as zero.
"""
from __future__ import annotations

import math
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from .planner import SlotDelta, catalog_entry, grade_costs, engineering_increments

MATERIAL_CATEGORIES = ("Raw", "Manufactured", "Encoded")
# In-game storage cap per material, by material grade.
GRADE_CAPS = {1: 300, 2: 250, 3: 200, 4: 150, 5: 100}
# Material trader rates inside one family: 1 unit of grade g+1 gives 3 of
# grade g; 6 units of grade g give 1 of grade g+1.
TRADE_DOWN_RATIO = 3
TRADE_UP_RATIO = 6

# Spellings used by the source/family data that differ from journal ids.
_ID_ALIASES = {
    "proprietarycomposites": "fedproprietarycomposites",
    "coredynamicscomposites": "fedcorecomposites",
    "flawedfocuscrystals": "uncutfocuscrystals",
    "adaptiveencyptorscapture": "adaptiveencryptors",  # typo in specials.json
}
# Guardian/Thargoid families are not exchanged like regular families.
_UNTRADEABLE_FAMILY_MARKERS = ("guardian", "thargoid")

GENERIC_SOURCES = {
    "Raw": "surface prospecting with the SRV, planetary deposits or mining",
    "Manufactured": (
        "signal sources (High Grade and Degraded Emissions), ship salvage, "
        "combat and mission rewards"
    ),
    "Encoded": "scanning ships, wakes, data points and beacons, mission rewards",
}


def _key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").casefold())


def _levenshtein(first: str, second: str) -> int:
    previous = list(range(len(second) + 1))
    for i, char_a in enumerate(first, 1):
        current = [i]
        for j, char_b in enumerate(second, 1):
            current.append(
                min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (char_a != char_b))
            )
        previous = current
    return previous[-1]


@dataclass(frozen=True)
class MaterialInfo:
    id: str
    name: str
    category: str | None
    grade: int | None
    family: str | None = None
    source: str | None = None
    tradeable: bool = False
    family_members: tuple[tuple[int, str], ...] = ()


class MaterialReference:
    """Journal material ids with display names, category, grade and family."""

    def __init__(
        self,
        template: dict[str, list[dict[str, Any]]],
        display_names: dict[str, str],
        family_maps: dict[str, dict[Any, dict[Any, Any]]],
    ):
        self._category: dict[str, str] = {}
        self._names: dict[str, str] = {}
        self._lookup: dict[str, str] = {}
        for category, entries in template.items():
            for entry in entries or []:
                material_id = str(entry.get("Name") or "").casefold()
                if not material_id:
                    continue
                self._category[material_id] = category
                self._names[material_id] = str(
                    entry.get("Name_Localised")
                    or display_names.get(material_id)
                    or material_id.title()
                )
        for material_id, name in self._names.items():
            self._lookup.setdefault(_key(material_id), material_id)
            self._lookup.setdefault(_key(name), material_id)
        for alias, target in _ID_ALIASES.items():
            if target in self._names:
                self._lookup.setdefault(alias, target)
        for raw_id, name in display_names.items():
            target = self._canonical(raw_id)
            if target:
                self._lookup.setdefault(_key(name), target)

        self._info: dict[str, MaterialInfo] = {}
        for category, families in family_maps.items():
            for family, grades in families.items():
                per_grade: dict[int, list[str]] = {}
                for grade, ids in grades.items():
                    if not isinstance(grade, int):
                        continue
                    canonical = sorted({c for c in (self._canonical(i) for i in ids) if c})
                    if canonical:
                        per_grade[grade] = canonical
                tradeable = all(len(ids) == 1 for ids in per_grade.values()) and not any(
                    marker in str(family).casefold() for marker in _UNTRADEABLE_FAMILY_MARKERS
                )
                members = (
                    tuple(sorted((grade, ids[0]) for grade, ids in per_grade.items()))
                    if tradeable else ()
                )
                label = f"raw category {family}" if category == "Raw" else str(family)
                source = grades.get("source")
                for grade, ids in per_grade.items():
                    for material_id in ids:
                        self._info[material_id] = MaterialInfo(
                            id=material_id,
                            name=self._names[material_id],
                            category=self._category.get(material_id, category),
                            grade=grade,
                            family=label,
                            source=str(source) if source else None,
                            tradeable=tradeable,
                            family_members=members,
                        )

    def _canonical(self, raw_id: Any) -> str | None:
        key = _key(raw_id)
        key = _ID_ALIASES.get(key, key)
        return key if key in self._names else None

    def name(self, material_id: str) -> str:
        return self._names.get(material_id, material_id)

    def info(self, material_id: str) -> MaterialInfo:
        return self._info.get(material_id) or MaterialInfo(
            id=material_id,
            name=self.name(material_id),
            category=self._category.get(material_id),
            grade=None,
        )

    def resolve(self, name: Any, candidates: Iterable[str] | None = None) -> str | None:
        """Journal id for an id or display name; exact, then unique partial,
        then close spelling. ``candidates`` restricts fuzzy matches."""
        key = _key(name)
        if not key:
            return None
        key = _ID_ALIASES.get(key, key)
        if key in self._lookup:
            return self._lookup[key]
        allowed = set(candidates) if candidates is not None else None
        pool = [
            (lookup_key, material_id)
            for lookup_key, material_id in self._lookup.items()
            if allowed is None or material_id in allowed
        ]
        partial = {material_id for lookup_key, material_id in pool if key in lookup_key}
        if len(partial) == 1:
            return partial.pop()
        scored = sorted((_levenshtein(key, lookup_key), material_id) for lookup_key, material_id in pool)
        if scored and scored[0][0] <= max(1, len(key) // 5):
            return scored[0][1]
        return None


def inventory_from_state(state: Any) -> dict[str, int] | None:
    """Material id -> count from the Materials projection; None when the
    game never reported the inventory."""
    if hasattr(state, "model_dump"):
        state = state.model_dump()
    if not isinstance(state, dict):
        return None
    inventory: dict[str, int] = {}
    stocked = False
    for category in MATERIAL_CATEGORIES:
        for entry in state.get(category) or []:
            if hasattr(entry, "model_dump"):
                entry = entry.model_dump()
            if not isinstance(entry, dict):
                continue
            material_id = str(entry.get("Name") or "").casefold()
            if not material_id:
                continue
            count = int(entry.get("Count") or 0)
            inventory[material_id] = inventory.get(material_id, 0) + count
            stocked = stocked or count > 0
    if not stocked and not state.get("LastUpdated"):
        return None
    return inventory


@dataclass
class StepNeed:
    step_id: str
    materials: dict[str, int] = field(default_factory=dict)
    unknown: list[str] = field(default_factory=list)
    engineering: bool = False

    def add(self, material_id: str, quantity: int) -> None:
        if quantity > 0:
            self.materials[material_id] = self.materials.get(material_id, 0) + quantity


def _special_recipe(specials: dict[str, Any], effect: str) -> dict[str, Any] | None:
    wanted = effect.casefold()
    for special_id, entry in specials.items():
        if not isinstance(entry, dict):
            continue
        if wanted in {str(special_id).casefold(), str(entry.get("edname") or "").casefold()}:
            components = entry.get("components")
            return components if isinstance(components, dict) and components else None
    return None


def step_need(
    step_id: str,
    delta: SlotDelta,
    catalog: dict[str, Any],
    specials: dict[str, Any],
    reference: MaterialReference,
) -> StepNeed:
    """Materials still required to reach one step's engineering target:
    the remaining increments of every blueprint grade, plus the experimental
    effect when it is not already applied on the same blueprint."""
    need = StepNeed(step_id)
    increments_plan = (
        engineering_increments(delta)
        if delta.target_blueprint and delta.status != "installed" else []
    )
    if increments_plan:
        need.engineering = True
        entry = catalog_entry(catalog, delta.target_blueprint)
        if entry is None:
            need.unknown.append(f"blueprint {delta.target_blueprint}")
        else:
            for grade, increments in increments_plan:
                costs, status = grade_costs(entry, grade)
                if status == "unknown":
                    need.unknown.append(f"{delta.target_blueprint} grade {grade}")
                    continue
                for cost in costs:
                    material_id = reference.resolve(
                        cost.get("material_id") or cost.get("material_localised")
                    )
                    if material_id is None:
                        need.unknown.append(f"material {cost.get('material_localised')}")
                        continue
                    need.add(material_id, int(cost.get("quantity") or 0) * increments)

    effect = delta.target_experimental
    effect_kept = (
        delta.status in {"installed", "grade_behind"}
        and (delta.current_experimental or "").casefold() == (effect or "").casefold()
    )
    if effect and not effect_kept:
        need.engineering = True
        recipe = _special_recipe(specials, effect)
        if recipe is None:
            need.unknown.append(f"experimental effect {effect}")
        else:
            for name, quantity in recipe.items():
                material_id = reference.resolve(name)
                if material_id is None:
                    need.unknown.append(f"material {name}")
                    continue
                need.add(material_id, int(quantity or 0))
    return need


@dataclass
class MaterialTotal:
    material_id: str
    needed: int = 0
    steps: list[str] = field(default_factory=list)


def aggregate_needs(needs: Iterable[StepNeed]) -> dict[str, MaterialTotal]:
    totals: dict[str, MaterialTotal] = {}
    for need in needs:
        for material_id, quantity in need.materials.items():
            total = totals.setdefault(material_id, MaterialTotal(material_id))
            total.needed += quantity
            if need.step_id not in total.steps:
                total.steps.append(need.step_id)
    return totals


@dataclass
class Shortfall:
    material_id: str
    name: str
    needed: int
    have: int
    steps: list[str] = field(default_factory=list)

    @property
    def missing(self) -> int:
        return max(0, self.needed - self.have)


def material_status(
    totals: dict[str, MaterialTotal],
    inventory: dict[str, int],
    reference: MaterialReference,
) -> list[Shortfall]:
    """Need vs stock for every required material, most missing first."""
    rows = [
        Shortfall(
            material_id=material_id,
            name=reference.name(material_id),
            needed=total.needed,
            have=inventory.get(material_id, 0),
            steps=list(total.steps),
        )
        for material_id, total in totals.items()
    ]
    rows.sort(key=lambda row: (-row.missing, -row.needed, row.name.casefold()))
    return rows


def shortfalls(
    totals: dict[str, MaterialTotal],
    inventory: dict[str, int],
    reference: MaterialReference,
) -> list[Shortfall]:
    return [row for row in material_status(totals, inventory, reference) if row.missing > 0]


@dataclass
class TradeOption:
    material_id: str
    name: str
    grade: int
    give: int
    receive: int


def trade_options(
    material_id: str,
    missing: int,
    inventory: dict[str, int],
    totals: dict[str, MaterialTotal],
    reference: MaterialReference,
) -> list[TradeOption]:
    """Same-family material trader exchanges using only stock the plan does
    not need, nearest grade first."""
    info = reference.info(material_id)
    target_grade = info.grade
    if not info.tradeable or target_grade is None or missing <= 0:
        return []
    remaining = missing
    options: list[TradeOption] = []
    # Nearest grade first; at equal distance spend abundant lower grades
    # before rarer higher grades.
    candidates = sorted(
        (member for member in info.family_members if member[1] != material_id),
        key=lambda member: (abs(member[0] - target_grade), member[0] > target_grade),
    )
    for grade, other in candidates:
        if remaining <= 0:
            break
        surplus = inventory.get(other, 0) - (totals[other].needed if other in totals else 0)
        if surplus <= 0:
            continue
        distance = abs(grade - target_grade)
        if grade > target_grade:
            per_unit = TRADE_DOWN_RATIO ** distance
            give = min(surplus, math.ceil(remaining / per_unit))
            receive = give * per_unit
        else:
            per_unit = TRADE_UP_RATIO ** distance
            receive = min(surplus // per_unit, remaining)
            give = receive * per_unit
        if receive <= 0:
            continue
        options.append(TradeOption(other, reference.name(other), grade, give, receive))
        remaining -= receive
    return options


def acquisition_plan(
    shortfall: Shortfall,
    inventory: dict[str, int],
    totals: dict[str, MaterialTotal],
    reference: MaterialReference,
) -> str:
    """Spoken plan to cover one shortfall: trades from surplus stock first,
    then the remaining quantity from the known sources."""
    info = reference.info(shortfall.material_id)
    kind = ", ".join(
        part for part in (
            f"{info.category} grade {info.grade}" if info.category and info.grade
            else info.category,
            f"family {info.family}" if info.family and info.category != "Raw" else None,
        ) if part
    )
    parts = [
        f"{shortfall.name}{f' ({kind})' if kind else ''}: missing {shortfall.missing} "
        f"(need {shortfall.needed}, have {shortfall.have})"
    ]
    options = trade_options(shortfall.material_id, shortfall.missing, inventory, totals, reference)
    if options:
        trader = f"{info.category.lower()} material trader" if info.category else "material trader"
        parts.append(
            f"trade surplus stock at a {trader}: "
            + ", ".join(
                f"{option.give} {option.name} (grade {option.grade}) for {option.receive}"
                for option in options
            )
        )
    rest = shortfall.missing - min(shortfall.missing, sum(option.receive for option in options))
    if rest > 0:
        if info.source:
            source = f"known farming spot for its family: {info.source}"
        else:
            source = GENERIC_SOURCES.get(info.category or "", "no source is known in the local data")
        parts.append(f"collect {rest}{' more' if options else ''} from {source}")
    cap = GRADE_CAPS.get(info.grade or 0)
    if cap and shortfall.needed > cap:
        parts.append(
            f"the plan needs {shortfall.needed} but storage is capped at {cap}, "
            "so collect and engineer in batches"
        )
    return "; ".join(parts) + "."
