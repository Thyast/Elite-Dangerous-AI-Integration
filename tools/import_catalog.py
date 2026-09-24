"""Reproducible import of the Ship-Build Assistant data catalog.

Fetches a pinned revision of EDCD/coriolis-data, normalizes it into the
data contract (provenance + cost_status, see Specs/04 §2.2), joins the
existing local assets, and writes:

  - src/assets/module_catalog.json     (versioned catalog, committed)
  - src/assets/catalog_report.json     (coverage / divergence report)

Run from the repository root:  python tools/import_catalog.py
Network is required only at build time; the runtime reads the committed asset.
"""
from __future__ import annotations

import json
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO = "https://raw.githubusercontent.com/EDCD/coriolis-data"
REVISION = "master"  # pin to a commit hash for reproducible builds
SOURCES = [{"id": "coriolis-data", "revision": REVISION, "license": "MIT"}]
ASSETS = Path(__file__).resolve().parents[1] / "src" / "assets"
OUT = ASSETS / "module_catalog.json"
REPORT = ASSETS / "catalog_report.json"

FILES = {
    "blueprints": "/modifications/blueprints.json",
    "modules_index": "/modules/index.json",
}


def fetch(path: str) -> dict:
    url = f"{REPO}/{REVISION}{path}"
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def norm_material(name: str) -> str:
    """Journal material symbol: lowercase alnum."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def load_local(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def build_material_index(materials: dict) -> tuple[dict[str, str], dict[str, str]]:
    """Two indexes: normalized journal symbol -> localized display, and
    normalized localized display -> journal symbol (covers drift between
    Coriolis display names and journal localised names)."""
    by_symbol: dict[str, str] = {}
    by_localised: dict[str, str] = {}
    for family, entries in materials.items():
        for entry in entries:
            symbol = norm_material(entry["Name"])
            localised = entry.get("Name_Localised") or entry["Name"].title()
            by_symbol[symbol] = localised
            by_localised[norm_material(localised)] = symbol
    return by_symbol, by_localised


def match_material(name: str, by_symbol: dict, by_localised: dict) -> str | None:
    """Resolve a Coriolis component name to a journal material symbol."""
    target = norm_material(name)
    if target in by_symbol:
        return target
    if target in by_localised:
        return by_localised[target]
    # containment fallback (longest journal symbol contained in the name
    # or the name contained in a journal symbol)
    best = None
    for symbol in by_symbol:
        if len(symbol) >= 6 and (symbol in target or target in symbol):
            if best is None or len(symbol) > len(best):
                best = symbol
    return best


def main() -> int:
    blueprints_raw = fetch(FILES["blueprints"])
    generated_at = datetime.now(timezone.utc).isoformat()

    materials = load_local(ASSETS / "materials.json")
    by_symbol, by_localised = build_material_index(materials)

    catalog_blueprints = []
    unknown_materials: set[str] = set()
    for family_key, blueprint in blueprints_raw.items():
        grades = {}
        for grade_str, grade_data in (blueprint.get("grades") or {}).items():
            components = grade_data.get("components") or {}
            costs = []
            for name, quantity in components.items():
                symbol = match_material(name, by_symbol, by_localised)
                if symbol is None:
                    unknown_materials.add(name)
                costs.append({
                    "material_id": symbol or norm_material(name),
                    "material_localised": by_symbol.get(symbol, name) if symbol else name,
                    "quantity": quantity,
                    "cost_status": "verified" if symbol else "unknown",
                    "source_name": name,
                })
            grades[str(int(grade_str))] = {
                "material_costs": costs,
                "features": grade_data.get("features") or {},
                "engineers": [],  # filled from the local asset below
            }
        catalog_blueprints.append({
            "id": family_key,
            "fdname": blueprint.get("fdname") or family_key,
            "name": blueprint.get("name") or family_key,
            "module_names": blueprint.get("modulename") or [],
            "grades": grades,
        })

    # Join engineers from the local asset (display-name based).
    engineers_asset = load_local(ASSETS / "ship_engineers.json")
    engineer_by_family: dict[str, dict[str, list[str]]] = {}
    modifications = load_local(ASSETS / "engineering_modifications.json")
    for blueprint_name, blueprint in modifications.items():
        for family, grade_data in blueprint.get("module_recipes", {}).items():
            fam_key = norm_material(family)
            for grade, data in grade_data.items():
                engineer_by_family.setdefault(fam_key, {}).setdefault(str(int(grade)), []).extend(
                    data.get("engineers", [])
                )
    matched_engineers = 0
    for entry in catalog_blueprints:
        for module_name in entry["module_names"]:
            fam_key = norm_material(module_name)
            per_grade = engineer_by_family.get(fam_key)
            if not per_grade:
                continue
            for grade, engineers in per_grade.items():
                if grade in entry["grades"]:
                    seen = set(entry["grades"][grade]["engineers"])
                    for engineer in engineers:
                        if engineer not in seen:
                            entry["grades"][grade]["engineers"].append(engineer)
                            matched_engineers += 1

    # Blueprint id -> journal fdname coverage report
    catalog = {
        "schema_version": 1,
        "generated_at": generated_at,
        "sources": SOURCES,
        "engineering_blueprints": sorted(catalog_blueprints, key=lambda e: e["id"]),
    }

    local_families = {norm_material(f) for bp in modifications.values() for f in bp.get("module_recipes", {})}
    catalog_families = {norm_material(entry["fdname"]) for entry in catalog_blueprints}
    catalog_module_names = {norm_material(m) for e in catalog_blueprints for m in e["module_names"]}

    report = {
        "schema_version": 1,
        "generated_at": generated_at,
        "sources": SOURCES,
        "counts": {
            "catalog_blueprints": len(catalog_blueprints),
            "local_blueprint_names": len(modifications),
            "local_module_families": len(local_families),
            "materials_known": len(by_symbol),
            "unknown_material_names": len(unknown_materials),
            "engineers_added_from_local_asset": matched_engineers,
        },
        "coverage": {
            "local_families_with_catalog_bp": sorted(local_families & catalog_module_names),
            "local_families_without_catalog_bp": sorted(local_families - catalog_module_names),
        },
        "unknown_materials": sorted(unknown_materials),
    }

    OUT.write_text(json.dumps(catalog, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    REPORT.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"catalog: {len(catalog_blueprints)} blueprints -> {OUT.name}")
    print(f"report: {report['counts']}")
    if unknown_materials:
        print(f"WARNING unknown materials: {sorted(unknown_materials)[:8]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
