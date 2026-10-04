"""Determinism and pure-function tests for the catalog import tool."""
import importlib.util
import json
from pathlib import Path

REPO = Path(__file__).parents[2]
TOOLS = REPO / "tools" / "import_catalog.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("import_catalog", TOOLS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_norm_material_matches_journal_symbols():
    module = _load_module()
    assert module.norm_material("Heat Dispersion Plate") == "heatdispersionplate"
    assert module.norm_material("Atypical Disrupted Wake Echoes") == "atypicaldisruptedwakeechoes"


def test_match_material_resolves_localised_drift():
    module = _load_module()
    materials = json.loads((REPO / "src" / "assets" / "materials.json").read_text(encoding="utf-8"))
    by_symbol, by_localised = module.build_material_index(materials)
    # Coriolis plural form vs journal singular localised
    assert module.match_material("Abnormal Compact Emissions Data", by_symbol, by_localised) == "compactemissionsdata"
    # exact localised match
    assert module.match_material("Exceptional Scrambled Emission Data", by_symbol, by_localised) == "scrambledemissiondata"
    # containment fallback for partial names
    assert module.match_material("Aberrant Shield Pattern Analysis", by_symbol, by_localised) == "shieldpatternanalysis"
    # genuinely unknown -> None (never treated as a zero cost)
    assert module.match_material("Unobtanium Dust", by_symbol, by_localised) is None


def test_technology_broker_requirements_resolve_to_covas_materials_and_commodities():
    from src.lib.actions.data import known_commodities

    materials = json.loads((REPO / "src" / "assets" / "materials.json").read_text(encoding="utf-8"))
    material_ids = {entry["Name"] for entries in materials.values() for entry in entries}
    broker_catalog = json.loads((REPO / "src" / "assets" / "technology_broker_unlocks.json").read_text(encoding="utf-8"))

    assert all(
        requirement["covas_id"] in material_ids
        for product in broker_catalog["products"]
        for requirement in product["requirements"]
        if requirement["type"] != "commodity"
    )
    assert all(
        requirement["covas_id"] in known_commodities
        and requirement["market_name"] in known_commodities
        and requirement["inventory_name"] == requirement["market_name"]
        for product in broker_catalog["products"]
        for requirement in product["requirements"]
        if requirement["type"] == "commodity"
    )


def test_technology_broker_blueprint_fragments_are_inventory_materials():
    materials = json.loads((REPO / "src" / "assets" / "materials.json").read_text(encoding="utf-8"))
    encoded_ids = {entry["Name"] for entry in materials["Encoded"]}
    assert {"guardian_moduleblueprint", "guardian_weaponblueprint"} <= encoded_ids


def test_technology_broker_known_journal_ids_are_not_broker_slugs():
    broker_catalog = json.loads((REPO / "src" / "assets" / "technology_broker_unlocks.json").read_text(encoding="utf-8"))
    products = {product["broker_id"]: product for product in broker_catalog["products"]}
    assert products["guardian_gauss_cannon_medium_fixed"]["journal_item_id"] == "Hpt_Guardian_GaussCannon_Fixed_Medium"
    assert products["guardian_plasma_charger_medium_fixed"]["journal_item_id"] == "Hpt_Guardian_PlasmaLauncher_Fixed_Medium"
    assert products["guardian_shard_cannon_medium_fixed"]["journal_item_id"] == "Hpt_Guardian_ShardCannon_Fixed_Medium"
    assert products["guardian_hybrid_power_plant"]["journal_item_status"] == "unverified"
    assert products["xg7_trident"]["journal_item_status"] == "not_ship_module"
    assert products["engineered_fsd_v1_size_5"]["journal_item_status"] == "unverified"


def test_technology_broker_module_i18n_keys_have_english_and_french_entries():
    translations_path = REPO / "ui" / "src" / "app" / "services" / "i18n-translations.ts"
    source = translations_path.read_text(encoding="utf-8")
    start = source.index("{", source.index("TRANSLATIONS"))
    tables = json.loads(source[start:source.rindex("}") + 1])
    broker_catalog = json.loads((REPO / "src" / "assets" / "technology_broker_unlocks.json").read_text(encoding="utf-8"))

    for product in broker_catalog["products"]:
        key = product.get("module_name_key")
        if key:
            assert key in tables["en"]["module"]
            assert key in tables["fr"]["module"]
            assert tables["en"]["module"][key] == product["name_en"]
            assert tables["fr"]["module"][key] == product["name_fr"]
        assert product["name_en"]
        assert product["name_fr"]
        assert product.get("journal_item_id") or product.get("journal_item_pattern") is None or product["journal_item_status"] in {"family_pattern", "unverified"}
