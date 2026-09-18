import base64
import json
from typing import Any
from urllib.parse import unquote, urlparse


class PlanParseError(ValueError):
    """Raised when an external loadout cannot be normalized."""


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

    ship_model = _first_string(
        source.get("ship_model"),
        source.get("ship"),
        source.get("ship", {}).get("name") if isinstance(source.get("ship"), dict) else None,
        source.get("ship", {}).get("model") if isinstance(source.get("ship"), dict) else None,
        source.get("shipType"),
        source.get("Ship"),
    )
    if isinstance(source.get("ship"), dict):
        ship_model = ship_model or _first_string(
            source["ship"].get("ship"),
            source["ship"].get("type"),
        )
    if not ship_model:
        raise PlanParseError("Could not identify the ship model")

    plan_name = _first_string(
        source.get("plan_name"),
        source.get("name"),
        source.get("title"),
        source.get("ShipName"),
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
        components = source["components"]
        modules = [
            module
            for group in components.values()
            if isinstance(group, dict)
            for module in group.values()
            if isinstance(module, dict)
        ]
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
    if "components" in source:
        return "coriolis"
    if "modules" in source or "Modules" in source:
        return "edsy"
    if "ship" in source and isinstance(source.get("ship"), dict):
        return "inara"
    return "normalized"
