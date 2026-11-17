"""Technology Broker recipe lookup and inventory comparison."""

import re
from typing import Any

from pydantic import BaseModel


def _key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold().replace("gardian", "guardian"))


def _pattern_matches(pattern: str, value: str) -> bool:
    escaped = re.escape(pattern)
    escaped = escaped.replace(r"\{size\}", r"\d+").replace(r"\{class\}", r"\d+")
    return re.fullmatch(escaped, value, re.IGNORECASE) is not None


def match_product(products: list[dict[str, Any]], query: str) -> dict[str, Any]:
    query = query.strip()
    if not query:
        raise ValueError("Name a Technology Broker product.")
    normalized = _key(query)
    exact = []
    for product in products:
        names = (
            product["broker_id"], product["name_en"], product["name_fr"],
            product.get("module_name_key") or "",
        )
        pattern = product.get("journal_item_pattern")
        if normalized in {_key(name) for name in names if name} or (
            pattern and _pattern_matches(pattern, query)
        ):
            exact.append(product)
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise ValueError("Ambiguous broker product: " + ", ".join(p["name_en"] for p in exact))
    partial = [p for p in products if normalized in _key(p["name_en"])]
    if len(partial) == 1:
        return partial[0]
    if partial:
        raise ValueError("Ambiguous broker product: " + ", ".join(p["name_en"] for p in partial))
    raise ValueError(f"No Technology Broker product matches '{query}'.")


def product_from_journal(products: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    """Only verified journal IDs/family patterns and unambiguous names are evidence."""
    for product in products:
        pattern = product.get("journal_item_pattern")
        if pattern and product.get("journal_item_status") in {"verified", "family_pattern"}:
            if _pattern_matches(pattern, name):
                return product
    try:
        product = match_product(products, name)
    except ValueError:
        return None
    return product if name.casefold().strip() in {
        product["name_en"].casefold(), product["name_fr"].casefold()
    } else None


def _state_dict(value: Any) -> dict[str, Any] | None:
    if isinstance(value, BaseModel):
        return value.model_dump()
    return value if isinstance(value, dict) else None


def recipe_needs(
    product: dict[str, Any], states: dict[str, Any] | None
) -> list[dict[str, Any]]:
    materials = _state_dict(states.get("Materials")) if states else None
    cargo = _state_dict(states.get("Cargo")) if states else None
    material_known = materials is not None and bool(materials.get("LastUpdated"))
    cargo_known = cargo is not None and bool(cargo.get("timestamp")) and cargo.get("Vessel") != "SRV"
    stock: dict[str, int] = {}
    if material_known:
        for category in ("Raw", "Manufactured", "Encoded"):
            for entry in materials.get(category) or []:
                item = _state_dict(entry)
                if item:
                    stock[_key(str(item.get("Name") or ""))] = int(item.get("Count") or 0)
    cargo_stock: dict[str, int] = {}
    if cargo_known:
        for entry in cargo.get("Inventory") or []:
            item = _state_dict(entry)
            if item and not item.get("Stolen"):
                name = item.get("Name")
                cargo_stock[_key(str(name or ""))] = cargo_stock.get(_key(str(name or "")), 0) + int(item.get("Count") or 0)
    result = []
    for requirement in product["requirements"]:
        commodity = requirement["type"] == "commodity"
        available = cargo_known if commodity else material_known
        owned = (
            cargo_stock.get(_key(requirement["inventory_name"]), 0)
            if commodity else stock.get(_key(requirement["covas_id"]), 0)
        ) if available else None
        result.append({
            **requirement,
            "have": owned,
            "missing": max(0, requirement["quantity"] - owned) if owned is not None else None,
        })
    return result
