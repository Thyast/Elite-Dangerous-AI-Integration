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
