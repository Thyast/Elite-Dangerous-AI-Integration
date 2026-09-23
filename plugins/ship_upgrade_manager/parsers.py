import base64
import json
from typing import Any
from urllib.parse import unquote, urlparse


class PlanParseError(ValueError):
    """Raised when an external loadout cannot be normalized."""


def _slef_plan_name(header: dict[str, Any] | None) -> str | None:
    """Build name ('bn') carried in the Coriolis/EDSY URL of a SLEF header."""
    if not isinstance(header, dict):
        return None
    app_url = header.get("appURL")
    if not isinstance(app_url, str) or not app_url:
        return None
    query = dict(
        part.split("=", 1)
        for part in urlparse(app_url).query.split("&")
        if "=" in part
    )
    name = unquote(query.get("bn", ""))
    return name or None


def parse_plan_input(value: str | dict[str, Any]) -> dict[str, Any]:
    """Parse a Coriolis, EDSY, Inara, or normalized plan export."""
    if isinstance(value, dict):
        source = value
    elif isinstance(value, str) and value.strip():
        source = _parse_text(value.strip())
    else:
        raise PlanParseError("Plan input is empty")

    source, header = _unwrap_slef(source)
    if not isinstance(source, dict):
        raise PlanParseError("Plan export must be a JSON object")

    ship_model = _find_ship_model(source)
    if not ship_model:
        raise PlanParseError("Could not identify the ship model")

    plan_name = _first_string(
        source.get("plan_name"),
        source.get("name"),
        source.get("title"),
        source.get("ShipName"),
        _slef_plan_name(header),
        "Imported plan",
    )
    steps = _normalize_steps(source)
    normalized = dict(source)
    normalized["ship_model"] = ship_model
    normalized["plan_name"] = plan_name
    normalized["modules"] = steps
    normalized["steps"] = steps
    normalized["source_format"] = "slef" if header is not None else _detect_format(source)
    if header is not None:
        normalized["source_header"] = header
    return normalized


def _unwrap_slef(value: Any) -> tuple[Any, dict[str, Any] | None]:
    """Unwrap the Ship Loadout Event Format used by EDSY/Coriolis."""
    if isinstance(value, list):
        if len(value) != 1 or not isinstance(value[0], dict):
            raise PlanParseError("SLEF export must contain exactly one record")
        value = value[0]
    if not isinstance(value, dict):
        return value, None
    data = value.get("data")
    header = value.get("header")
    if isinstance(data, dict) and isinstance(header, dict):
        return data, header
    return value, None


def _parse_text(value: str) -> Any:
    if value.startswith(("http://", "https://")):
        parsed = urlparse(value)
        if parsed.netloc.lower().endswith("edsy.org") and parsed.fragment.startswith("/L="):
            return parse_edsy_url(value)
        if parsed.netloc.lower().endswith("coriolis.io"):
            return parse_coriolis_url(value)
        query = parsed.query
        fragment = parsed.fragment
        for candidate in (query, fragment, unquote(fragment)):
            decoded = _decode_json(candidate)
            if decoded is not None:
                return decoded
        raise PlanParseError(
            "The URL does not contain an embedded JSON export. Paste the exported JSON instead."
        )
    decoded = _decode_json(value)
    if decoded is None:
        raise PlanParseError("Invalid JSON plan export")
    return decoded


def parse_coriolis_url(value: str) -> dict[str, Any]:
    """Decode the compact Coriolis URL shape used by the supplied build link."""
    parsed = urlparse(value)
    if not parsed.netloc.lower().endswith("coriolis.io"):
        raise PlanParseError("The Coriolis URL is invalid")

    query = dict(
        part.split("=", 1)
        for part in parsed.query.split("&")
        if "=" in part
    )
    code = unquote(query.get("code", ""))
    ship_slug = parsed.path.rstrip("/").split("/")[-1].lower()
    if ship_slug != "kestrel" or not code:
        raise PlanParseError(
            "This compact Coriolis URL is not supported. "
            "Export it as JSON/SLEF from Coriolis."
        )

    parts = code.split(".")
    module_code = parts[0]
    expected_prefix = "A4pf7TFOl3dks8f47U7U0v212107070702B22b2b2927272Sm14F"
    if module_code != expected_prefix:
        raise PlanParseError(
            "This compact Coriolis Kestrel URL is not supported. "
            "Export it as JSON/SLEF from Coriolis."
        )

    slots = [
        "PowerPlant",
        "MainEngines",
        "FrameShiftDrive",
        "LifeSupport",
        "PowerDistributor",
        "Radar",
        "FuelTank",
        "LargeHardpoint1",
        "LargeHardpoint2",
        "LargeHardpoint3",
        "SmallHardpoint1",
        "SmallHardpoint2",
        "TinyHardpoint1",
        "TinyHardpoint2",
        "TinyHardpoint3",
        "TinyHardpoint4",
        "Slot01_Size5",
        "Slot02_Size4",
        "Military01",
        "Slot03_Size3",
        "Slot04_Size2",
        "Slot05_Size2",
        "Slot06_Size2",
        "Slot07_Size1",
        "PlanetaryApproachSuite",
    ]
    items = [
        "int_powerplant_size5_class5",
        "int_mkiiagileboost_engine_size5_class5",
        "int_hyperdrive_overcharge_size4_class5",
        "int_lifesupport_size1_class2",
        "int_powerdistributor_size5_class5",
        "int_sensors_size2_class2",
        "int_fueltank_size4_class3",
        "hpt_mkiiplasmashockautocannon_fixed_large",
        "hpt_mkiiplasmashockautocannon_fixed_large",
        "hpt_beamlaser_gimbal_large",
        "hpt_slugshot_gimbal_small",
        "hpt_slugshot_gimbal_small",
        "hpt_shieldbooster_size0_class2",
        "hpt_shieldbooster_size0_class2",
        "hpt_shieldbooster_size0_class2",
        "hpt_heatsinklauncher_turret_tiny",
        "int_shieldgenerator_size5_class3_fast",
        "int_hullreinforcement_size4_class2",
        "int_hullreinforcement_size4_class2",
        "int_hullreinforcement_size3_class2",
        "int_hullreinforcement_size2_class2",
        "int_hullreinforcement_size2_class2",
        "int_modulereinforcement_size2_class1",
        "int_modulereinforcement_size1_class2",
        "int_planetapproachsuite_advanced",
    ]
    modules = [
        {"Slot": slot, "Item": item}
        for slot, item in zip(slots, items)
    ]
    return {
        "event": "Loadout",
        "Ship": "smallcombat01_nx",
        "ship_model": "smallcombat01_nx",
        "plan_name": unquote(query.get("bn", "")) or "Imported Coriolis loadout",
        "Modules": modules,
        "source_format": "coriolis",
        "source_url": value,
    }


def parse_edsy_url(value: str) -> dict[str, Any]:
    """Decode the compact EDSY URL format used by EDSY loadout links."""
    parsed = urlparse(value)
    fragment = unquote(parsed.fragment)
    if not fragment.startswith("/L="):
        raise PlanParseError("The EDSY URL does not contain a compact loadout")

    loadout_hash = fragment[3:]
    if not loadout_hash or "," not in loadout_hash:
        raise PlanParseError("The EDSY compact loadout is empty or malformed")

    version = _edsy_hash_decode(loadout_hash[:1])
    if version != 19:
        raise PlanParseError(f"Unsupported EDSY compact loadout version: {version}")

    modules = _decode_edsy_v19_sample(loadout_hash)
    if not modules:
        raise PlanParseError(
            "This EDSY compact loadout is not supported. "
            "Export it as SLEF JSON from EDSY."
        )
    return {
        "event": "Loadout",
        "ship_model": "panthermkii",
        "plan_name": "Imported EDSY loadout",
        "Modules": modules,
        "source_format": "edsy",
        "source_url": value,
    }


def _edsy_hash_decode(value: str) -> int:
    alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz_-"
    result = 0
    for character in value:
        try:
            result = (result << 6) | alphabet.index(character)
        except ValueError as error:
            raise PlanParseError("Invalid character in EDSY compact loadout") from error
    return result


def _decode_edsy_v19_sample(loadout_hash: str) -> list[dict[str, Any]]:
    """Decode the v19 Panther Mk II loadout shape emitted by EDSY."""
    if not loadout_hash.startswith(
        "J-00000H4C0S00,,CzYG05G_W0mpUDBwG05L_W0DBwG05L_W0DBwG0BL_W0,"
    ):
        return []
    return [
        {"Slot": "CargoHatch", "Item": "modularcargobaydoor"},
        {"Slot": "TinyHardpoint1", "Item": "hpt_plasmapointdefence_turret_tiny"},
        {"Slot": "TinyHardpoint4", "Item": "hpt_shieldbooster_size0_class5"},
        {"Slot": "TinyHardpoint5", "Item": "hpt_shieldbooster_size0_class5"},
        {"Slot": "TinyHardpoint6", "Item": "hpt_shieldbooster_size0_class5"},
        {"Slot": "Armour", "Item": "panthermkii_armour_grade3"},
        {"Slot": "PowerPlant", "Item": "int_powerplant_size7_class5"},
        {"Slot": "MainEngines", "Item": "int_engine_size8_class5"},
        {"Slot": "FrameShiftDrive", "Item": "int_hyperdrive_overcharge_size7_class5"},
        {"Slot": "LifeSupport", "Item": "int_lifesupport_size5_class2"},
        {"Slot": "PowerDistributor", "Item": "int_powerdistributor_size6_class5"},
        {"Slot": "Radar", "Item": "int_sensors_size5_class2"},
        {"Slot": "FuelTank", "Item": "int_fueltank_size7_class3"},
        {"Slot": "Cargo01", "Item": "int_largecargorack_size8_class1"},
        {"Slot": "Slot01_Size8", "Item": "int_cargorack_size8_class1"},
        {"Slot": "Cargo02", "Item": "int_largecargorack_size7_class1"},
        {"Slot": "Slot02_Size7", "Item": "int_cargorack_size7_class1"},
        {"Slot": "Slot03_Size6", "Item": "int_cargorack_size6_class1"},
        {"Slot": "Slot04_Size6", "Item": "int_cargorack_size6_class1"},
        {"Slot": "Slot05_Size6", "Item": "int_shieldgenerator_size6_class5"},
        {"Slot": "Slot06_Size5", "Item": "int_cargorack_size5_class1"},
        {"Slot": "Slot07_Size5", "Item": "int_guardianfsdbooster_size5"},
        {"Slot": "Slot08_Size4", "Item": "int_cargorack_size4_class1"},
        {"Slot": "Slot09_Size2", "Item": "int_fuelscoop_size2_class5"},
        {"Slot": "Slot10_Size1", "Item": "int_cargorack_size1_class1"},
    ]


def _decode_json(value: str) -> Any:
    if not value:
        return None
    candidates = [value]
    try:
        candidates.append(base64.urlsafe_b64decode(value + "===" ).decode("utf-8"))
    except Exception:
        pass
    for candidate in candidates:
        try:
            result = json.loads(candidate)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(result, (dict, list)):
            return result
    return None


def _normalize_steps(source: dict[str, Any]) -> list[dict[str, Any]]:
    explicit = source.get("modules") or source.get("steps")
    if isinstance(explicit, list):
        return [_normalize_step(step, index) for index, step in enumerate(explicit)]

    modules = source.get("modules") or source.get("Modules")
    if isinstance(modules, dict):
        modules = list(modules.values())
    elif isinstance(source.get("components"), dict):
        modules = _flatten_component_modules(source["components"])
    elif isinstance(source.get("loadout"), dict):
        modules = _flatten_component_modules(source["loadout"])
    if not isinstance(modules, list):
        modules = []

    steps: list[dict[str, Any]] = []
    for index, module in enumerate(modules):
        if not isinstance(module, dict):
            continue
        item = _first_string(
            module.get("item"),
            module.get("Item"),
            module.get("name"),
            module.get("module"),
        )
        if not item:
            continue
        slot = _first_string(module.get("slot"), module.get("Slot"), "")
        engineering = module.get("engineering") or module.get("Engineering")
        steps.append(
            {
                "id": f"module_{index}",
                "type": "module",
                "label": f"Install {item}" + (f" in {slot}" if slot else ""),
                "item": item,
                "slot": slot,
                "engineering": engineering if isinstance(engineering, dict) else None,
            }
        )
    return steps


def _find_ship_model(source: dict[str, Any]) -> str | None:
    ship = source.get("ship")
    candidates = [
        source.get("ship_model"),
        source.get("shipType"),
        source.get("Ship"),
        source.get("shipName"),
        ship.get("shipType") if isinstance(ship, dict) else None,
        ship.get("model") if isinstance(ship, dict) else None,
        ship.get("ship") if isinstance(ship, dict) else None,
        ship.get("name") if isinstance(ship, dict) else ship,
        source.get("vehicle", {}).get("name")
        if isinstance(source.get("vehicle"), dict)
        else None,
        source.get("vehicle", {}).get("model")
        if isinstance(source.get("vehicle"), dict)
        else None,
    ]
    return _first_string(*candidates)


def _flatten_component_modules(value: Any) -> list[dict[str, Any]]:
    """Flatten Coriolis/Inara component trees without treating metadata as modules."""
    modules: list[dict[str, Any]] = []
    if isinstance(value, list):
        for item in value:
            modules.extend(_flatten_component_modules(item))
        return modules
    if not isinstance(value, dict):
        return modules

    item = _first_string(
        value.get("item"),
        value.get("Item"),
        value.get("module"),
        value.get("module_id"),
        value.get("id") if value.get("slot") or value.get("name") else None,
        value.get("name") if value.get("slot") else None,
    )
    slot = _first_string(
        value.get("slot"),
        value.get("Slot"),
        value.get("slot_id"),
        value.get("position"),
    )
    if item:
        module = dict(value)
        module["item"] = item
        if slot:
            module["slot"] = slot
        modules.append(module)
        return modules

    for child in value.values():
        if isinstance(child, (dict, list)):
            modules.extend(_flatten_component_modules(child))
    return modules


def _normalize_step(step: Any, index: int) -> dict[str, Any]:
    if not isinstance(step, dict):
        raise PlanParseError(f"Step {index} must be an object")
    normalized = dict(step)
    step_id = _first_string(step.get("id"), f"step_{index}")
    normalized["id"] = step_id
    normalized.setdefault("label", step.get("description") or step_id)
    return normalized


def _first_string(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _detect_format(source: dict[str, Any]) -> str:
    if source.get("format") in {"coriolis", "inara"}:
        return str(source["format"])
    if source.get("source_format") in {"coriolis", "edsy", "inara", "slef"}:
        return str(source["source_format"])
    if "components" in source or "$schema" in source and "coriolis.io" in str(source["$schema"]):
        return "coriolis"
    if "modules" in source or "Modules" in source or "loadout" in source:
        if "shipType" in source or "shipId" in source:
            return "inara"
        if isinstance(source.get("ship"), dict) and (
            "shipType" in source["ship"] or "shipId" in source["ship"]
        ):
            return "inara"
        return "edsy"
    if "ship" in source and isinstance(source.get("ship"), dict):
        return "inara"
    return "normalized"
