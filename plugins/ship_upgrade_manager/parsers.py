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
