"""Honest build planner for the Ship Upgrade Manager (spec: Specs/03 + 04).

Pure module — no COVAS:NEXT imports, fully testable. The planner compares a
target build (imported plan steps) against the ship's current loadout and
produces an ordered, cited action list per the spec's honesty rules:

- a missing/unknown material cost is flagged, never treated as zero;
- blueprint targets are joined against the versioned catalog by journal
  `BlueprintName` (e.g. `FSD_LongRange`);
- engineer visits are grounded in the local engineer asset (system,
  station, coordinates); unknown engineers are refused, not invented.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

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


def norm_slot(slot: str) -> str:
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

    kind: str  # install_module | station_purchase | engineer_visit | apply_blueprint | experimental | blocking
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


@dataclass
class StationSearchRun:
    """Per-calculation station query memo; deliberately has no persistence."""

    search: Any
    cache: dict[Any, dict[str, Any]] = field(default_factory=dict)

    def __call__(self, filters: list[dict[str, Any]], **kwargs) -> dict[str, Any]:
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


def _station_module_filter(item: str, module_filter=None) -> dict[str, Any] | None:
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


def _grade_letter(module_class: int | None) -> str | None:
    if module_class is None:
        return None
    return CLASS_LETTERS.get(int(module_class))


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


def _item_grade(item: str) -> tuple[str | None, str | None]:
    """Size and letter grade encoded in an FD item id (5A / D / None)."""
    match = re.search(r"_size(\d+)_class(\d+)", item)
    if match:
        size, module_class = int(match.group(1)), int(match.group(2))
        letter = _grade_letter(module_class)
        return f"{size}{letter or '?'}", letter
    class_match = re.search(r"_class(\d+)$", item)
    if class_match:
        return None, _grade_letter(int(class_match.group(1)))
    return None, None


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


def _catalog_entry(
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


def _grade_costs(entry: dict[str, Any], grade: int) -> tuple[list[dict[str, Any]], str]:
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
        for engineer_id, engineer in engineer_registry.items():
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


def _distance_ly(origin: tuple | None, coords: dict | None) -> float | None:
    if not origin or not coords:
        return None
    dx = origin[0] - float(coords.get("x", 0))
    dy = origin[1] - float(coords.get("y", 0))
    dz = origin[2] - float(coords.get("z", 0))
    return round((dx * dx + dy * dy + dz * dz) ** 0.5, 1)


def render_chain(actions: list[PlannedAction]) -> str:
    """Render the ordered action list as a spoken chain (spec §16.1).

    'First: … Then: … After that: …' — each step with its system and grade
    when relevant, skipping blocking steps (which are mentioned at the end)."""
    if not actions:
        return "Nothing pending — the build matches the plan."
    blocking = [action for action in actions if action.kind == "blocking"]
    pending = [action for action in actions if action.kind != "blocking"]
    if not pending:
        return f"Cannot compute: {blocking[0].target}." if blocking else "Nothing pending."
    connectives = ["First", "Then", "After that"]
    parts = []
    for index, action in enumerate(pending):
        connective = connectives[min(index, len(connectives) - 1)]
        detail = f"{action.kind.replace('_', ' ')}: {action.target}"
        if action.system:
            detail += f" ({action.system}"
            if action.distance_ly is not None:
                detail += f", {action.distance_ly} ly"
            detail += ")"
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
    if blocking:
        result += f" Note: {blocking[0].target}."
    return result


def build_advice(
    deltas: list[SlotDelta],
    catalog: dict[str, Any],
    engineer_registry: dict[str, Any],
    current_position: tuple | None = None,
    ship_name: str | None = None,
    station_search=None,
    module_filter=None,
    inventory: dict[str, int] | None = None,
) -> list[PlannedAction]:
    """Ordered action list for the deltas, following the spec honesty rules.

    Ordering: module acquisition first, then engineering grades ascending,
    grouped by engineer (one visit per engineer, closest first). Every
    action carries citations; unknown material costs are flagged.

    With an ``inventory`` (material id -> count), each blueprint grade gets
    a ``stock_status`` of ``sufficient`` or ``missing``, consuming the stock
    cumulatively in route order."""
    actions: list[PlannedAction] = []
    visited: set[str] = set()
    remaining_stock = dict(inventory) if inventory is not None else None
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
            request = _station_module_filter(delta.target_item, module_filter)
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

    route_filter_keys = {
        key for stop in station_route for key in stop.module_filter_keys
    }
    if station_search_error:
        actions.append(
            PlannedAction(
                kind="blocking",
                slot=None,
                target=station_search_error,
                cost_status="unknown",
                rationale="No purchase location is claimed for modules without a Spansh match.",
                citations=["Spansh station_finder"],
            )
        )
    for stop in station_route:
        actions.append(
            PlannedAction(
                kind="station_purchase",
                slot=None,
                target=", ".join(stop.modules),
                system=stop.system,
                station=stop.station,
                distance_ly=stop.distance_ly,
                cost_status="unknown",
                stock_status=stop.stock_status,
                rationale=(
                    "Spansh lists these modules in the station catalog; current stock and prices "
                    "must be checked in-game."
                ),
                citations=stop.citations,
            )
        )

    for delta in sorted(
        deltas,
        key=lambda d: (d.status == "installed", d.slot),
    ):
        if delta.status == "installed":
            continue
        if delta.status in {"missing", "module_different"} and station_search is not None:
            request = request_by_slot.get(delta.slot)
            request_key = _module_filter_key(request) if request else None
            if request_key is not None and request_key in route_filter_keys:
                if not delta.target_blueprint:
                    continue
            elif request_key is not None:
                actions.append(
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
                actions.append(
                    PlannedAction(
                        kind="blocking",
                        slot=delta.slot,
                        target=f"cannot map {delta.target_item} to a Spansh module name",
                        cost_status="unknown",
                        rationale="No station search was issued because the module mapping is ambiguous.",
                        citations=["target build", "station_finder module catalog"],
                    )
                )
            if not delta.target_blueprint:
                continue
        elif delta.status in {"missing", "module_different"} and station_search is None:
            actions.append(
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
        entry = _catalog_entry(catalog, delta.target_blueprint)
        if entry is None:
            actions.append(
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
        for grade, increments in engineering_increments(delta):
            costs, status = _grade_costs(entry, grade)
            engineers = _engineers_for_blueprint(entry, grade, engineer_registry)
            engineers = sorted(
                engineers,
                key=lambda e: (
                    _distance_ly(current_position, e.get("coords"))
                    if _distance_ly(current_position, e.get("coords")) is not None
                    else float("inf")
                ),
            )
            if not engineers:
                actions.append(
                    PlannedAction(
                        kind="blocking",
                        slot=delta.slot,
                        target=f"no engineer data for blueprint {delta.target_blueprint} grade {grade}",
                        cost_status=status,
                        rationale=(
                            f"Grade {grade} requires materials {costs or 'unknown'} but the "
                            "local engineer registry covers no engineer for this blueprint."
                        ),
                        citations=["module_catalog.json", "ship_engineers.json"],
                    )
                )
                continue
            for engineer in engineers:
                distance = _distance_ly(current_position, engineer.get("coords"))
                if engineer["name"] not in visited:
                    visited.add(engineer["name"])
                    actions.append(
                        PlannedAction(
                            kind="engineer_visit",
                            slot=delta.slot,
                            target=engineer["name"],
                            system=engineer.get("system"),
                            station=engineer.get("station"),
                            distance_ly=distance,
                            rationale=f"Visit for blueprint {delta.target_blueprint}",
                            citations=["ship_engineers.json"],
                        )
                    )
                    break
            stock_status = "unknown"
            if remaining_stock is not None and status == "verified":
                stock_status = "sufficient"
                for cost in costs:
                    material_id = str(cost.get("material_id") or "").casefold()
                    quantity = int(cost.get("quantity") or 0) * increments
                    available = remaining_stock.get(material_id, 0)
                    if available < quantity:
                        stock_status = "missing"
                    remaining_stock[material_id] = max(0, available - quantity)
            actions.append(
                PlannedAction(
                    kind="apply_blueprint",
                    slot=delta.slot,
                    target=delta.target_blueprint,
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
    return actions
