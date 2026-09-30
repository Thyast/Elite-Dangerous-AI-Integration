"""Honest build planner for the Ship Upgrade Manager (spec: Specs/03 + 04).

Pure module — no COVAS:NEXT imports, fully testable. The planner compares a
target build (imported plan steps) against the ship's current loadout and
produces an ordered, cited action list per the spec's honesty rules:

- a missing/unknown material cost is flagged, never treated as zero;
- blueprint targets are joined against the versioned catalog by journal
  `BlueprintName` (e.g. `FSD_LongRange`);
- engineer visits are grounded in the local engineer asset (system,
  station, coordinates); unknown engineers are refused, not invented;
- the stops (outfitting stations, material traders, engineers) are ordered
  as the shortest open path from the commander's position (see route.py).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from .route import RouteStop, best_insertion, coords_of, distance_ly, order_stops, refine

# E:D letter grades: class 1=E ... class 5=A.
CLASS_LETTERS = {1: "E", 2: "D", 3: "C", 4: "B", 5: "A"}


def normalize_item(item: str) -> str:
    """Journal/SLEF symbolic id normalization (same rules as the plugin)."""
    cleaned = item.strip().lower()
    if cleaned.startswith("$"):
        cleaned = cleaned[1:]
    if cleaned.lower().endswith("_name;"):
        cleaned = cleaned[: -len("_name;")]
    return cleaned


def norm_slot(slot: str | None) -> str:
    return (slot or "").strip().lower()


def module_family(item: str) -> str:
    """Module family from an FD id (strip prefix and FD slot/class suffixes)."""
    cleaned = normalize_item(item)
    for prefix in ("hpt_cr_", "hpt_", "int_"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):]
            break
    cleaned = re.sub(r"_(?:fixed|gimbal|turret)(?:_(?:small|medium|large|huge|tiny))?$", "", cleaned)
    # Preserve a known trailing variant after class (e.g. int_engine_size5_class5_agile).
    variant_match = re.search(r"_size\d+_class\d+_([a-z0-9_]+)$", cleaned)
    if variant_match:
        prefix = cleaned[:variant_match.start()]
        cleaned = f"{prefix}_{variant_match.group(1)}"
    else:
        cleaned = re.sub(r"_size\d+_class\d+$", "", cleaned)
    cleaned = re.sub(r"_(?:small|medium|large|huge)$", "", cleaned)
    return cleaned.strip("_")


def canonical_item(item: str) -> str:
    """Canonicalize known ship-specific module aliases without fuzzy matching."""
    normalized = normalize_item(item)
    aliases = {
        "smallcombat01_nx_armour_reactive": "reactive_surface_composite",
        "reactive_armour": "reactive_surface_composite",
        "reactive armour": "reactive_surface_composite",
        "int_reactive_armour": "reactive_surface_composite",
        "int_armour_reactive": "reactive_surface_composite",
        "reactive_surface_composite": "reactive_surface_composite",
        "reactive surface composite": "reactive_surface_composite",
    }
    return aliases.get(normalized, aliases.get(normalized.replace("_", " "), normalized))


def experimental_effect(engineering: dict[str, Any] | None) -> str | None:
    """Experimental effect id of an engineering dict (journal or plan keys)."""
    engineering = engineering or {}
    value = (
        engineering.get("ExperimentalEffect")
        or engineering.get("experimental_effect")
        or engineering.get("experimentalEffect")
    )
    if not value:
        return None
    return str(value).strip() or None


def _module_filter_key(request: dict[str, Any]) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    def values(name: str) -> tuple[str, ...]:
        value = request.get(name) or []
        if isinstance(value, (str, int)):
            value = [value]
        return tuple(sorted(str(item).casefold() for item in value))
    return (
        str(request.get("name", "")).casefold(),
        values("class"),
        values("rating"),
    )


def blueprint_effect(blueprint_name: str) -> str:
    """Journal BlueprintName effect part: 'FSD_LongRange' -> 'longrange'."""
    parts = normalize_item(blueprint_name).split("_")
    return parts[-1] if parts else ""


@dataclass
class SlotDelta:
    """Difference between the target build and the current ship for a slot."""

    slot: str
    target_item: str
    target_blueprint: str | None
    target_level: int | None
    current_item: str | None
    current_blueprint: str | None
    current_level: int | None
    status: str  # installed | grade_behind | blueprint_different | missing | module_different
    current_quality: float | None = None  # 0.0..1.0 from journal Engineering.Quality
    grades_needed: list[int] = field(default_factory=list)
    materials_missing: list[dict[str, Any]] = field(default_factory=list)
    blueprint_known: bool = False
    target_experimental: str | None = None
    current_experimental: str | None = None


@dataclass
class PlannedAction:
    """One ordered, citable action of the recommendation."""

    kind: str  # install_module | station_purchase | material_trade | engineer_visit | apply_blueprint | experimental | blocking
    slot: str | None
    target: str  # item, engineer name, or blocking reason
    system: str | None = None
    station: str | None = None
    distance_ly: float | None = None
    grade: int | None = None
    increments: int = 1
    materials: list[dict[str, Any]] = field(default_factory=list)
    cost_status: str = "verified"
    stock_status: str = "unknown"
    rationale: str = ""
    citations: list[str] = field(default_factory=list)
    # Route stops only: distance from the previous stop (from the commander
    # for the first stop); None when either end has no known coordinates.
    leg_distance_ly: float | None = None
    # Engineer visits only: None when unlocked (or unknown), else the
    # journal progress status of a locked engineer used as a fallback.
    access: str | None = None


@dataclass
class PlannedTrade:
    """One material trader exchange from surplus stock (materials.trade_options)."""

    category: str  # Raw | Manufactured | Encoded
    give_id: str
    give_name: str
    give_quantity: int
    receive_id: str
    receive_name: str
    receive_quantity: int


@dataclass
class StationRouteStop:
    station_id: str
    station: str
    system: str
    distance_ly: float | None
    modules: list[str]
    module_filter_keys: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = field(default_factory=list)
    stock_status: str = "unknown"
    citations: list[str] = field(default_factory=lambda: ["Spansh station_finder"])
    coords: dict[str, Any] | None = None


@dataclass
class StationSearchRun:
    """Per-calculation station query memo; deliberately has no persistence."""

    search: Any
    cache: dict[Any, dict[str, Any]] = field(default_factory=dict)

    def __call__(self, filters: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        explicit = kwargs.get("cache_key")
        key = explicit if explicit is not None else tuple(sorted(_module_filter_key(item) for item in filters))
        if key not in self.cache:
            self.cache[key] = self.search(filters, **kwargs)
        return self.cache[key]


def _station_identity(station: dict[str, Any]) -> str:
    return str(station.get("id") or f"{station.get('system', '').casefold()}::{station.get('name', '').casefold()}")


def _station_result_modules(station: dict[str, Any]) -> list[dict[str, Any]]:
    return station.get("modules") or []


def _station_covers_filter(station: dict[str, Any], request: dict[str, Any]) -> bool:
    wanted = _module_filter_key(request)
    if any(_module_filter_key(matched) == wanted for matched in station.get("matched_filters", [])):
        return True
    # Conservative response-side check for Spansh responses that do not
    # include matched_filters (e.g. local Outfitting projection).
    for offered in _station_result_modules(station):
        name = str(offered.get("name", "")).casefold()
        classes = offered.get("class", [])
        ratings = offered.get("rating", [])
        if isinstance(classes, (str, int)):
            classes = [classes]
        if isinstance(ratings, str):
            ratings = [ratings]
        name_ok = not wanted[0] or wanted[0] in name
        class_ok = not wanted[1] or bool(set(wanted[1]) & {str(v).casefold() for v in classes})
        rating_ok = not wanted[2] or bool(set(wanted[2]) & {str(v).casefold() for v in ratings})
        if name_ok and class_ok and rating_ok:
            return True
    return False


def _merge_station(existing: dict[str, Any], incoming: dict[str, Any]) -> None:
    for key in ("modules", "matched_filters"):
        values = existing.setdefault(key, [])
        seen = {
            _module_filter_key(item) if key == "matched_filters" else (
                str(item.get("name", "")).casefold(),
                str(item.get("class", "")).casefold(),
                str(item.get("rating", "")).casefold(),
            )
            for item in values
        }
        for item in incoming.get(key, []):
            item_key = _module_filter_key(item) if key == "matched_filters" else (
                str(item.get("name", "")).casefold(),
                str(item.get("class", "")).casefold(),
                str(item.get("rating", "")).casefold(),
            )
            if item_key not in seen:
                values.append(item)
                seen.add(item_key)


def _select_station_route(module_filters: list[dict[str, Any]], stations: list[dict[str, Any]]) -> tuple[list[StationRouteStop], list[dict[str, Any]]]:
    remaining = {_module_filter_key(item): item for item in module_filters}
    route: list[StationRouteStop] = []
    while remaining:
        candidates = []
        for station in stations:
            keys = [key for key, request in remaining.items() if _station_covers_filter(station, request)]
            if keys:
                distance = station.get("distance_ly")
                distance_key = float(distance) if isinstance(distance, (int, float)) else float("inf")
                candidates.append((-len(keys), distance_key, _station_identity(station), station, keys))
        if not candidates:
            break
        _, _, identity, station, keys = min(candidates)
        route.append(StationRouteStop(
            station_id=identity,
            station=str(station.get("name", "Unknown station")),
            system=str(station.get("system", "Unknown system")),
            distance_ly=station.get("distance_ly"),
            modules=[str(remaining[key].get("name", "")) for key in keys],
            module_filter_keys=keys,
            stock_status=str(station.get("stock_status", "unknown")),
            coords=station.get("coords"),
        ))
        for key in keys:
            remaining.pop(key, None)
    return route, list(remaining.values())


def select_station_route(module_filters: list[dict[str, Any]], search: Any) -> tuple[list[StationRouteStop], list[dict[str, Any]]]:
    """Grouped search + uncovered-module fallback + greedy set-cover route.

    A list argument is a pre-fetched station set (pure route-selection helper,
    retained for callers/tests); a callable or StationSearchRun performs the
    grouped and fallback Spansh queries.
    """
    unique = {_module_filter_key(item): item for item in module_filters}
    requested = list(unique.values())
    if not requested:
        return [], []
    if isinstance(search, list):
        return _select_station_route(requested, search)
    run = search if isinstance(search, StationSearchRun) else StationSearchRun(search)
    grouped = run(requested, cache_key="grouped")
    stations: dict[str, dict[str, Any]] = {}
    for station in grouped.get("stations", []) if grouped.get("status") == "ok" else []:
        stations[_station_identity(station)] = station
    covered = {key for key, request in unique.items() if any(_station_covers_filter(s, request) for s in stations.values())}
    for key, request in unique.items():
        if key in covered:
            continue
        result = run([request], cache_key="module:" + repr(key))
        if result.get("status") != "ok":
            continue
        for station in result.get("stations", []):
            identity = _station_identity(station)
            if identity in stations:
                _merge_station(stations[identity], station)
            else:
                stations[identity] = station
        covered = {k for k, req in unique.items() if any(_station_covers_filter(s, req) for s in stations.values())}
    return _select_station_route(requested, list(stations.values()))


def select_station_route_from_search(module_filters: list[dict[str, Any]], run: StationSearchRun) -> tuple[list[StationRouteStop], list[dict[str, Any]]]:
    """Compatibility alias for tests/callers already using StationSearchRun."""
    return select_station_route(module_filters, run)


def _station_module_filter(item: str) -> dict[str, Any] | None:
    """Map an FD item id to the exact host station_finder allow-list name.

    No fuzzy guessing: every mapping is a deliberate family alias and is
    validated against ``known_modules`` before a Spansh request is sent.
    """
    name = normalize_item(item)
    family = re.sub(r"[^a-z0-9]", "", module_family(name))
    if canonical_item(name) == "reactive_surface_composite":
        family = "reactivesurfacecomposite"
    aliases = {
        "reactivesurfacecomposite": "Reactive Surface Composite",
        "powerplant": "Power Plant", "engine": "Thrusters",
        "engineagile": "Mk II Agile Boost Thrusters", "mkiiagileboostengine": "Mk II Agile Boost Thrusters",
        "hyperdrive": "Frame Shift Drive", "hyperdriveovercharge": "Frame Shift Drive (SCO)",
        "lifesupport": "Life Support", "powerdistributor": "Power Distributor",
        "fueltank": "Fuel Tank", "fuelscoop": "Fuel Scoop",
        "shieldgenerator": "Shield Generator", "shieldgeneratorfast": "Bi-Weave Shield Generator",
        "shieldgeneratorstrong": "Prismatic Shield Generator", "shieldcellbank": "Shield Cell Bank",
        "hullreinforcement": "Hull Reinforcement Package", "modulereinforcement": "Module Reinforcement Package",
        "guardianmodulereinforcement": "Guardian Module Reinforcement",
        "guardianhullreinforcement": "Guardian Hull Reinforcement", "guardianshieldreinforcement": "Guardian Shield Reinforcement",
        "shieldbooster": "Shield Booster", "heatsinklauncher": "Heat Sink Launcher",
        "chafflauncher": "Chaff Launcher", "electroniccountermeasure": "Electronic Countermeasure",
        "beamlaser": "Beam Laser", "pulselaser": "Pulse Laser", "pulselaserburst": "Burst Laser",
        "plasmaaccelerator": "Plasma Accelerator", "mkiiplasmashockautocannon": "Mk II Plasma Shock Accelerator",
        "plasmashockautocannon": "Mk II Plasma Shock Accelerator", "multicannon": "Multi-Cannon",
        "mkiimulticannon": "Multi-Cannon", "slugshot": "Fragment Cannon", "railgun": "Rail Gun",
        "cannon": "Cannon", "mininglaser": "Mining Laser", "miningabrasionblaster": "Abrasion Blaster",
        "miningseismicchargelauncher": "Seismic Charge Launcher", "miningsubsurfacedisplacement": "Sub-Surface Displacement Missile",
        "guardiangausscannon": "Guardian Gauss Cannon", "guardianplasmalauncher": "Guardian Plasma Charger",
        "guardianshardcannon": "Guardian Shard Cannon", "atmulticannon": "AX Multi-Cannon",
        "atmissiletile": "AX Missile Rack", "remotereleaseflak": "Remote Release Flechette Launcher",
        "flakmortar": "Remote Release Flak Launcher", "basicmissilerack": "Seeker Missile Rack",
        "dumbfiremissilerack": "Missile Rack", "torpedopylon": "Torpedo Pylon", "minelauncher": "Mine Launcher",
        "fighterbay": "Fighter Hangar", "buggybay": "Planetary Vehicle Hangar",
        "repairer": "Auto Field-Maintenance Unit", "dockingcomputerstandard": "Standard Docking Computer",
        "dockingcomputeradvanced": "Advanced Docking Computer", "supercruiseassist": "Supercruise Assist",
        "detailedsurfacescanner": "Detailed Surface Scanner", "cargorack": "Cargo Rack",
        "cargorackcorrosionresistant": "Corrosion Resistant Cargo Rack", "passengercabin": "Passenger Cabin",
        "guardianfsdbooster": "Guardian FSD Booster", "refinery": "Refinery",
    }
    display = aliases.get(family)
    if display is None:
        return None
    try:
        from lib.actions.data import known_modules
    except ImportError:
        try:
            from src.lib.actions.data import known_modules
        except ImportError:
            return None
    canonical = next((known for known in known_modules if known.casefold() == display.casefold()), None)
    if canonical is None:
        return None
    result: dict[str, Any] = {"name": canonical}
    size = re.search(r"_size(\d+)", name)
    if size and int(size.group(1)) > 0:
        result["class"] = [str(int(size.group(1)))]
    elif name.startswith("hpt_"):
        hardpoint_size = re.search(r"_(small|medium|large|huge)(?:_|$)", name)
        if hardpoint_size:
            result["class"] = [{"small": "1", "medium": "2", "large": "3", "huge": "4"}[hardpoint_size.group(1)]]
    module_class = re.search(r"_class(\d+)", name)
    if module_class:
        rating = {1: "E", 2: "D", 3: "C", 4: "B", 5: "A"}.get(int(module_class.group(1)))
        if rating:
            result["rating"] = [rating]
    return result


# Journal Quality of a completed grade is 1.0; tolerate float noise.
QUALITY_COMPLETE = 0.999


def grade_reached(level: Any, quality: Any, target_level: Any) -> bool:
    """Whether an engineered module reaches a target grade at full effect:
    a higher grade, or the target grade completed (Quality 1.0). A missing
    Quality (plans, older data) counts as complete."""
    if target_level is None:
        return True
    if not isinstance(level, (int, float)):
        return False
    target = float(target_level)
    if level != target:
        return level > target
    return not isinstance(quality, (int, float)) or quality >= QUALITY_COMPLETE


def engineering_increments(delta: SlotDelta) -> list[tuple[int, int]]:
    """(grade, increments) still to apply to reach the blueprint target.

    Grade N takes N increments, each at the catalog recipe cost (journal
    EngineerCraft Quality rises by 1/N per increment). An incomplete current
    grade (Quality < 1) must be finished before the next grade unlocks."""
    plan: list[tuple[int, int]] = []
    if (
        delta.status == "grade_behind"
        and delta.current_level
        and delta.current_quality is not None
        and delta.current_quality < QUALITY_COMPLETE
    ):
        remaining = round((1 - delta.current_quality) * delta.current_level)
        if remaining > 0:
            plan.append((delta.current_level, remaining))
    plan.extend((grade, grade) for grade in delta.grades_needed)
    return plan


def analyze_build(
    target_steps: list[dict[str, Any]],
    loadout_modules: list[dict[str, Any]],
) -> list[SlotDelta]:
    """Compare the target build against the ship's current loadout per slot.

    Each SlotDelta carries the fractional `quality` (0.0..1.0 from the
    journal Engineering.Quality field) when the module is installed."""
    loadout_by_slot: dict[str, dict[str, Any]] = {}
    for module in loadout_modules:
        loadout_by_slot[norm_slot(module.get("Slot"))] = module

    deltas: list[SlotDelta] = []
    for step in target_steps:
        slot = norm_slot(step.get("slot"))
        target_item = canonical_item(str(step.get("item") or ""))
        engineering = step.get("engineering") or {}
        target_blueprint = normalize_item(str(engineering.get("BlueprintName") or "")) or None
        target_level = int(float(engineering["Level"])) if engineering.get("Level") is not None else None

        current = loadout_by_slot.get(slot)
        if current is None:
            # fall back to a loadout module with the same target item
            for module in loadout_modules:
                if canonical_item(str(module.get("Item") or "")) == target_item:
                    current = module
                    break
        current_item = canonical_item(str(current.get("Item") or "")) if current else None
        current_engineering = (current or {}).get("Engineering") or {}
        current_blueprint = normalize_item(str(current_engineering.get("BlueprintName") or "")) or None
        current_level_raw = current_engineering.get("Level")
        current_level = int(float(current_level_raw)) if current_level_raw is not None else None
        current_quality = current_engineering.get("Quality")

        if current is None:
            status = "missing"
        elif current_item == target_item:
            if not target_blueprint:
                status = "installed"
            elif current_blueprint == target_blueprint and grade_reached(
                current_level, current_quality, target_level
            ):
                status = "installed"
            elif current_blueprint == target_blueprint:
                status = "grade_behind"
            else:
                status = "blueprint_different"
        else:
            status = "module_different"

        if status == "grade_behind" and target_level and current_level:
            grades_needed = list(range(current_level + 1, target_level + 1))
        elif target_blueprint and status in {"missing", "module_different", "blueprint_different"}:
            grades_needed = list(range(1, (target_level or 0) + 1))
        else:
            grades_needed = []

        deltas.append(
            SlotDelta(
                slot=slot,
                target_item=target_item,
                target_blueprint=target_blueprint,
                target_level=target_level,
                current_item=current_item,
                current_blueprint=current_blueprint,
                current_level=current_level,
                current_quality=float(current_quality) if isinstance(current_quality, (int, float)) else None,
                status=status,
                grades_needed=grades_needed,
                target_experimental=experimental_effect(engineering),
                current_experimental=experimental_effect(current_engineering),
            )
        )
    return deltas


def catalog_entry(
    catalog: dict[str, Any], blueprint_name: str | None
) -> dict[str, Any] | None:
    """Join a journal BlueprintName against the catalog by fdname effect key."""
    if not blueprint_name:
        return None
    normalized = normalize_item(blueprint_name)
    for entry in catalog.get("engineering_blueprints", []):
        entry_key = normalize_item(str(entry.get("fdname") or entry.get("id") or ""))
        if normalized == entry_key or normalized.endswith(entry_key) or entry_key.endswith(normalized):
            return entry
    return None


def grade_costs(entry: dict[str, Any], grade: int) -> tuple[list[dict[str, Any]], str]:
    """Material costs for one blueprint grade, with an aggregate cost_status.

    Any unknown material makes the whole grade 'unknown' (never zero)."""
    grade_data = (entry.get("grades") or {}).get(str(grade)) or {}
    costs = grade_data.get("material_costs") or []
    status = "verified"
    for cost in costs:
        if cost.get("cost_status") == "unknown":
            status = "unknown"
            break
    if not costs:
        status = "unknown"
    return costs, status


def _engineers_for_blueprint(
    entry: dict[str, Any], grade: int, engineer_registry: dict[str, Any]
) -> list[dict[str, Any]]:
    """Ground the engineer visit in the local registry (system, station, coords)."""
    names = ((entry.get("grades") or {}).get(str(grade)) or {}).get("engineers") or []
    found: list[dict[str, Any]] = []
    for name in names:
        for engineer in engineer_registry.values():
            if engineer.get("Engineer") == name:
                found.append(
                    {
                        "name": name,
                        "system": engineer.get("Location"),
                        "station": engineer.get("Workshop"),
                        "coords": engineer.get("Coords"),
                    }
                )
                break
    return found


def _distance_ly(origin: tuple[float, ...] | None, coords: dict[str, Any] | None) -> float | None:
    if not origin or not coords:
        return None
    dx = origin[0] - float(coords.get("x", 0))
    dy = origin[1] - float(coords.get("y", 0))
    dz = origin[2] - float(coords.get("z", 0))
    return round((dx * dx + dy * dy + dz * dz) ** 0.5, 1)


ROUTE_STOP_KINDS = {"station_purchase", "material_trade", "engineer_visit"}


def render_chain(actions: list[PlannedAction]) -> str:
    """Render the ordered action list as a spoken chain (spec §16.1).

    'First: … Then: … After that: …' — each step with its system and grade
    when relevant, skipping blocking steps (which are mentioned at the end).
    Route stops give the distance from the previous stop (from the commander
    for the first one) and the chain ends with the total route length."""
    if not actions:
        return "Nothing pending — the build matches the plan."
    blocking = [action for action in actions if action.kind == "blocking"]
    pending = [action for action in actions if action.kind != "blocking"]
    if not pending:
        return f"Cannot compute: {blocking[0].target}." if blocking else "Nothing pending."
    connectives = ["First", "Then", "After that"]
    parts = []
    stops = 0
    total = 0.0
    known_legs = 0
    unknown_legs = 0
    for index, action in enumerate(pending):
        connective = connectives[min(index, len(connectives) - 1)]
        detail = f"{action.kind.replace('_', ' ')}: {action.target}"
        is_stop = action.kind in ROUTE_STOP_KINDS
        if action.system:
            detail += f" ({action.system}"
            if is_stop and action.leg_distance_ly is not None:
                detail += f", {action.leg_distance_ly} ly " + ("from you" if stops == 0 else "further")
            elif action.distance_ly is not None:
                detail += f", {action.distance_ly} ly"
            detail += ")"
        if is_stop:
            stops += 1
            if action.leg_distance_ly is None:
                unknown_legs += 1
            else:
                known_legs += 1
                total += action.leg_distance_ly
        if action.access:
            detail += f" (not unlocked yet: {action.access})"
        if action.kind == "material_trade" and action.materials:
            detail += ", trade " + ", ".join(
                f"{trade['give_quantity']} {trade['give_name']} for "
                f"{trade['receive_quantity']} {trade['receive_name']}"
                for trade in action.materials
            )
        if action.grade:
            detail += f", grade {action.grade}"
            if action.kind == "apply_blueprint" and action.increments > 1:
                detail += f" ({action.increments} increments)"
        if action.cost_status == "unknown":
            detail += " (materials unverified)"
        elif action.kind == "apply_blueprint" and action.stock_status == "missing":
            detail += " (materials missing)"
        parts.append(f"{connective}: {detail}")
    result = ". ".join(parts) + "."
    if stops > 1 and known_legs:
        result += f" Total route: {round(total, 1)} ly"
        if unknown_legs:
            result += " plus legs of unknown length"
        result += "."
    if blocking:
        result += f" Note: {blocking[0].target}."
    return result


# Engineer access: an unlocked engineer, or any engineer when the journal
# EngineerProgress is unavailable ("unknown"), is usable without warning.
_USABLE_ACCESS = {"unlocked", "unknown"}


def _engineer_access(name: str, engineer_progress: dict[str, dict[str, Any]] | None) -> str:
    """'unlocked', 'barred', 'unknown' (no progress data), or the locked
    journal status ('Known', 'Invited', 'Acquainted', 'Not discovered')."""
    if engineer_progress is None:
        return "unknown"
    status = (engineer_progress.get(name) or {}).get("Progress")
    if status == "Unlocked":
        return "unlocked"
    if status == "Barred":
        return "barred"
    return str(status) if status else "Not discovered"


@dataclass
class _EngineeringUnit:
    """Grades of one slot's blueprint that a single engineer visit covers."""

    delta: SlotDelta
    entry: dict[str, Any]
    grades: list[tuple[int, int]]  # (grade, increments)
    candidates: dict[str, dict[str, Any]]  # engineer name -> registry location


def _default_material_id(cost: dict[str, Any]) -> str | None:
    return str(cost.get("material_id") or "").casefold() or None


def _engineering_units(
    delta: SlotDelta,
    entry: dict[str, Any],
    engineer_registry: dict[str, Any],
    engineer_progress: dict[str, dict[str, Any]] | None,
    blocking: list[PlannedAction],
) -> list[_EngineeringUnit]:
    """Group the remaining grades of a blueprint behind usable engineers.

    Unlocked engineers are preferred; a locked one is only kept for a grade
    no unlocked engineer can apply. Barred engineers are never proposed.
    When one engineer can apply every remaining grade the blueprint is a
    single unit (one visit), otherwise each grade is its own unit."""
    per_grade: list[tuple[int, int, dict[str, dict[str, Any]]]] = []
    for grade, increments in engineering_increments(delta):
        costs, status = grade_costs(entry, grade)
        known = _engineers_for_blueprint(entry, grade, engineer_registry)
        engineers = {
            engineer["name"]: engineer for engineer in known
            if _engineer_access(engineer["name"], engineer_progress) != "barred"
        }
        if not engineers:
            reason = (
                f"every engineer for blueprint {delta.target_blueprint} grade {grade} has barred you"
                if known else f"no engineer data for blueprint {delta.target_blueprint} grade {grade}"
            )
            blocking.append(
                PlannedAction(
                    kind="blocking",
                    slot=delta.slot,
                    target=reason,
                    cost_status=status,
                    rationale=(
                        f"Grade {grade} requires materials {costs or 'unknown'} but no "
                        "usable engineer in the local registry applies this blueprint."
                    ),
                    citations=["module_catalog.json", "ship_engineers.json", "EngineerProgress"],
                )
            )
            continue
        usable = {
            name: engineer for name, engineer in engineers.items()
            if _engineer_access(name, engineer_progress) in _USABLE_ACCESS
        }
        per_grade.append((grade, increments, usable or engineers))
    if not per_grade:
        return []
    shared = set.intersection(*(set(candidates) for _grade, _increments, candidates in per_grade))
    if shared:
        return [
            _EngineeringUnit(
                delta,
                entry,
                [(grade, increments) for grade, increments, _candidates in per_grade],
                {name: per_grade[0][2][name] for name in sorted(shared)},
            )
        ]
    return [
        _EngineeringUnit(delta, entry, [(grade, increments)], candidates)
        for grade, increments, candidates in per_grade
    ]


def _assign_engineers(
    units: list[_EngineeringUnit],
    origin: tuple[float, float, float] | None,
    engineer_progress: dict[str, dict[str, Any]] | None,
) -> dict[str, tuple[dict[str, Any], list[_EngineeringUnit]]]:
    """Greedy set cover: the engineer covering the most remaining units
    first (then usable before locked, then closest), one visit each."""
    uncovered = list(units)
    assignments: dict[str, tuple[dict[str, Any], list[_EngineeringUnit]]] = {}
    while uncovered:
        counts: dict[str, int] = {}
        info: dict[str, dict[str, Any]] = {}
        for unit in uncovered:
            for name, engineer in unit.candidates.items():
                counts[name] = counts.get(name, 0) + 1
                info[name] = engineer

        def rank(name: str) -> tuple[int, int, float, str]:
            distance = distance_ly(origin, coords_of(info[name].get("coords")))
            usable = _engineer_access(name, engineer_progress) in _USABLE_ACCESS
            return (-counts[name], 0 if usable else 1,
                    distance if distance is not None else float("inf"), name)

        chosen = min(counts, key=rank)
        assignments[chosen] = (info[chosen], [unit for unit in uncovered if chosen in unit.candidates])
        uncovered = [unit for unit in uncovered if chosen not in unit.candidates]
    return assignments


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(value, 1)


def _distance_or_inf(row: dict[str, Any]) -> float:
    distance = row.get("distance_ly")
    return float(distance) if isinstance(distance, (int, float)) else float("inf")


def build_advice(
    deltas: list[SlotDelta],
    catalog: dict[str, Any],
    engineer_registry: dict[str, Any],
    current_position: tuple[float, ...] | None = None,
    ship_name: str | None = None,
    station_search: Callable[..., dict[str, Any]] | None = None,
    inventory: dict[str, int] | None = None,
    engineer_progress: dict[str, dict[str, Any]] | None = None,
    trades: list[PlannedTrade] | None = None,
    trader_search: Callable[[str], dict[str, Any]] | None = None,
    material_id_of: Callable[[dict[str, Any]], str | None] | None = None,
) -> list[PlannedAction]:
    """Ordered action list for the deltas, following the spec honesty rules.

    Blocking notes and location-less module installs come first, then the
    optimised route: outfitting stations (greedy set cover), material traders
    and engineers (one visit per engineer, set cover over the remaining
    grades) ordered as the shortest open path from ``current_position`` with
    precedence constraints — a module is bought before it is engineered, and
    a trader is visited before the engineers consuming its exchanges.

    ``engineer_progress`` (engineer name -> journal EngineerProgress entry)
    restricts engineers to unlocked ones, falling back to a locked engineer
    (flagged in ``access``) only when no unlocked one applies a grade;
    ``None`` means the progress is unknown and nothing is filtered.

    ``trades`` (exchanges from surplus stock) add one trader stop per
    material category, found with ``trader_search(category)`` and chosen for
    the smallest detour.

    With an ``inventory`` (material id -> count), each blueprint grade gets
    a ``stock_status`` of ``sufficient`` or ``missing``, consuming the stock
    (and applying the planned trades) cumulatively in route order.
    ``material_id_of`` maps a catalog cost to an inventory id."""
    material_id_of = material_id_of or _default_material_id
    origin = coords_of(current_position)
    leading: list[PlannedAction] = []
    acquisition_slots = [
        delta for delta in deltas
        if delta.status in {"missing", "module_different"}
    ]
    station_route: list[StationRouteStop] = []
    uncovered_module_filters: list[dict[str, Any]] = []
    station_search_error: str | None = None
    request_by_slot: dict[str, dict[str, Any]] = {}
    module_filters: list[dict[str, Any]] = []
    filter_keys: set[tuple[str, tuple[str, ...], tuple[str, ...]]] = set()
    if acquisition_slots and station_search is not None:
        for delta in acquisition_slots:
            request = _station_module_filter(delta.target_item)
            if request is None:
                continue
            request_by_slot[delta.slot] = request
            key = _module_filter_key(request)
            if key not in filter_keys:
                filter_keys.add(key)
                module_filters.append(request)
        if module_filters:
            station_route, uncovered_module_filters = select_station_route(
                module_filters, station_search
            )
            if uncovered_module_filters:
                station_search_error = "Spansh returned no matching station for one or more modules."
        elif acquisition_slots:
            station_search_error = "Target module names could not be mapped to Spansh's station catalog."

    if station_search_error:
        leading.append(
            PlannedAction(
                kind="blocking",
                slot=None,
                target=station_search_error,
                cost_status="unknown",
                rationale="No purchase location is claimed for modules without a Spansh match.",
                citations=["Spansh station_finder"],
            )
        )

    stops: list[RouteStop] = [
        RouteStop(f"station:{stop.station_id}", coords_of(stop.coords), payload=stop)
        for stop in station_route
    ]
    stop_key_by_filter = {
        key: f"station:{stop.station_id}"
        for stop in station_route for key in stop.module_filter_keys
    }
    purchase_stop_by_slot: dict[str, str] = {}
    units: list[_EngineeringUnit] = []
    for delta in sorted(deltas, key=lambda d: d.slot):
        if delta.status == "installed":
            continue
        if delta.status in {"missing", "module_different"} and station_search is not None:
            request = request_by_slot.get(delta.slot) or {}
            request_key = _module_filter_key(request) if request else None
            if request_key is not None and request_key in stop_key_by_filter:
                purchase_stop_by_slot[delta.slot] = stop_key_by_filter[request_key]
            elif request_key is not None:
                leading.append(
                    PlannedAction(
                        kind="blocking",
                        slot=delta.slot,
                        target=f"no matching outfitting station found for {request['name']}",
                        cost_status="unknown",
                        rationale="Spansh returned no station match; no purchase location is claimed.",
                        citations=["Spansh station_finder"],
                    )
                )
            else:
                leading.append(
                    PlannedAction(
                        kind="blocking",
                        slot=delta.slot,
                        target=f"cannot map {delta.target_item} to a Spansh module name",
                        cost_status="unknown",
                        rationale="No station search was issued because the module mapping is ambiguous.",
                        citations=["target build", "station_finder module catalog"],
                    )
                )
        elif delta.status in {"missing", "module_different"}:
            leading.append(
                PlannedAction(
                    kind="install_module",
                    slot=delta.slot,
                    target=delta.target_item,
                    rationale=(
                        f"Target module {delta.target_item} differs from the installed "
                        f"{delta.current_item or '(empty slot)'}"
                        + (f" on {ship_name}" if ship_name else "")
                    ),
                    citations=["target build", "current Loadout"],
                )
            )
        if not delta.target_blueprint:
            continue
        entry = catalog_entry(catalog, delta.target_blueprint)
        if entry is None:
            leading.append(
                PlannedAction(
                    kind="blocking",
                    slot=delta.slot,
                    target=f"blueprint {delta.target_blueprint} is not in the local catalog",
                    cost_status="unknown",
                    rationale=(
                        f"Materials for {delta.target_blueprint} are unknown: the planner "
                        "refuses to invent a material list. Enable an updated catalog."
                    ),
                    citations=["module_catalog.json"],
                )
            )
            continue
        units.extend(
            _engineering_units(delta, entry, engineer_registry, engineer_progress, leading)
        )

    engineer_stops: list[RouteStop] = []
    for name, (engineer, assigned) in _assign_engineers(units, origin, engineer_progress).items():
        materials_used = {
            material_id_of(cost)
            for unit in assigned for grade, _increments in unit.grades
            for cost in grade_costs(unit.entry, grade)[0]
        } - {None}
        engineer_stops.append(
            RouteStop(
                f"engineer:{name}",
                coords_of(engineer.get("coords")),
                after={
                    purchase_stop_by_slot[unit.delta.slot]
                    for unit in assigned if unit.delta.slot in purchase_stop_by_slot
                },
                payload=(name, engineer, assigned, materials_used),
            )
        )
    stops.extend(engineer_stops)

    trades_by_category: dict[str, list[PlannedTrade]] = {}
    for trade in trades or []:
        if trade.category:
            trades_by_category.setdefault(trade.category, []).append(trade)
    trader_stations: dict[str, list[dict[str, Any]]] = {}
    if trader_search is not None:
        for category in sorted(trades_by_category):
            result = trader_search(category) or {}
            stations = result.get("stations", []) if result.get("status") == "ok" else []
            if stations:
                trader_stations[category] = stations
                continue
            leading.append(
                PlannedAction(
                    kind="blocking",
                    slot=None,
                    target=f"no {category.lower()} material trader found",
                    cost_status="unknown",
                    rationale="The planned exchanges from surplus stock have no trader location.",
                    citations=["Spansh station_finder"],
                )
            )
    for category in trader_stations:
        received = {trade.receive_id for trade in trades_by_category[category]}
        dependents = [stop for stop in engineer_stops if stop.payload[3] & received] or engineer_stops
        for stop in dependents:
            stop.after.add(f"trader:{category}")

    order = order_stops(stops, origin)
    for category in sorted(trader_stations):
        key = f"trader:{category}"
        stations = trader_stations[category]
        candidates = [
            RouteStop(f"{key}:{_station_identity(station)}", coords_of(station.get("coords")), payload=station)
            for station in stations
        ]
        dependents = [index for index, stop in enumerate(order) if key in stop.after]
        latest = min(dependents) if dependents else None
        choice = best_insertion(order, origin, candidates, latest)
        if choice is None:
            station = min(stations, key=_distance_or_inf)
            trader, index = RouteStop(key, None), latest if latest is not None else len(order)
        else:
            candidate, index = choice
            station = candidate.payload
            trader = RouteStop(key, candidate.coords)
        trader.payload = (category, station)
        order.insert(index, trader)
    if trader_stations:
        order = refine(order, origin)

    remaining_stock = dict(inventory) if inventory is not None else None
    route_actions: list[PlannedAction] = []
    position = origin
    for stop in order:
        leg = _rounded(distance_ly(position, stop.coords))
        position = stop.coords
        if stop.key.startswith("station:"):
            purchase: StationRouteStop = stop.payload
            route_actions.append(
                PlannedAction(
                    kind="station_purchase",
                    slot=None,
                    target=", ".join(purchase.modules),
                    system=purchase.system,
                    station=purchase.station,
                    distance_ly=purchase.distance_ly,
                    leg_distance_ly=leg,
                    cost_status="unknown",
                    stock_status=purchase.stock_status,
                    rationale=(
                        "Spansh lists these modules in the station catalog; current stock and prices "
                        "must be checked in-game."
                    ),
                    citations=purchase.citations,
                )
            )
        elif stop.key.startswith("trader:"):
            category, station = stop.payload
            category_trades = trades_by_category[category]
            if remaining_stock is not None:
                for trade in category_trades:
                    remaining_stock[trade.give_id] = max(
                        0, remaining_stock.get(trade.give_id, 0) - trade.give_quantity
                    )
                    remaining_stock[trade.receive_id] = (
                        remaining_stock.get(trade.receive_id, 0) + trade.receive_quantity
                    )
            route_actions.append(
                PlannedAction(
                    kind="material_trade",
                    slot=None,
                    target=f"{category} material trader",
                    system=station.get("system"),
                    station=station.get("name"),
                    distance_ly=station.get("distance_ly"),
                    leg_distance_ly=leg,
                    materials=[
                        {
                            "give_id": trade.give_id,
                            "give_name": trade.give_name,
                            "give_quantity": trade.give_quantity,
                            "receive_id": trade.receive_id,
                            "receive_name": trade.receive_name,
                            "receive_quantity": trade.receive_quantity,
                        }
                        for trade in category_trades
                    ],
                    rationale="Exchange surplus stock before the engineers that consume it.",
                    citations=["Spansh station_finder", "Materials inventory"],
                )
            )
        else:
            name, engineer, assigned, _materials = stop.payload
            access = _engineer_access(name, engineer_progress)
            blueprints = list(dict.fromkeys(str(unit.delta.target_blueprint) for unit in assigned))
            route_actions.append(
                PlannedAction(
                    kind="engineer_visit",
                    slot=assigned[0].delta.slot,
                    target=name,
                    system=engineer.get("system"),
                    station=engineer.get("station"),
                    distance_ly=_distance_ly(current_position, engineer.get("coords")),
                    leg_distance_ly=leg,
                    access=None if access in _USABLE_ACCESS else access,
                    rationale=f"Visit for blueprint {', '.join(blueprints)}",
                    citations=["ship_engineers.json"]
                    + (["EngineerProgress"] if engineer_progress is not None else []),
                )
            )
            for unit in sorted(assigned, key=lambda item: item.delta.slot):
                delta = unit.delta
                for grade, increments in unit.grades:
                    costs, status = grade_costs(unit.entry, grade)
                    stock_status = "unknown"
                    if remaining_stock is not None and status == "verified":
                        stock_status = "sufficient"
                        for cost in costs:
                            material_id = material_id_of(cost)
                            if material_id is None:
                                continue
                            quantity = int(cost.get("quantity") or 0) * increments
                            available = remaining_stock.get(material_id, 0)
                            if available < quantity:
                                stock_status = "missing"
                            remaining_stock[material_id] = max(0, available - quantity)
                    route_actions.append(
                        PlannedAction(
                            kind="apply_blueprint",
                            slot=delta.slot,
                            target=str(delta.target_blueprint),
                            grade=grade,
                            increments=increments,
                            materials=costs,
                            cost_status=status,
                            stock_status=stock_status,
                            rationale=(
                                f"Apply grade {grade} of {delta.target_blueprint} "
                                f"({increments} increment{'s' if increments > 1 else ''}, "
                                "each at the listed material cost)"
                                + (" (materials unknown — verify in game)" if status == "unknown" else "")
                            ),
                            citations=["module_catalog.json", "current Loadout"],
                        )
                    )
    return leading + route_actions
