"""Tests for the generated data catalog (Specs/04 data contract).

Validates the committed asset `src/assets/module_catalog.json` against the
contract in `Specs/04-data-sources-and-cache.md` §2.2: schema version,
provenance on every blueprint, verified-vs-unknown cost status, and the rule
that an absent cost is never interpreted as zero.
"""
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parents[2]
sys.path.insert(0, str(REPO / "src"))

CATALOG_PATH = REPO / "src" / "assets" / "module_catalog.json"
REPORT_PATH = REPO / "src" / "assets" / "catalog_report.json"


@pytest.fixture(scope="module")
def catalog() -> dict:
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def report() -> dict:
    return json.loads(REPORT_PATH.read_text(encoding="utf-8"))


def test_catalog_has_schema_version_and_provenance_sources(catalog: dict):
    assert catalog["schema_version"] == 1
    assert catalog["sources"], "provenance sources list must not be empty"
    for source in catalog["sources"]:
        assert source["id"]
        assert source["revision"]
        assert source["license"]


def test_catalog_blueprints_carry_provenance_and_costs(catalog: dict):
    blueprints = catalog["engineering_blueprints"]
    assert len(blueprints) >= 80
    for blueprint in blueprints:
        assert blueprint["id"], "blueprint id required"
        assert blueprint["fdname"], "journal fdname required for id joins"
        for grade in blueprint["grades"].values():
            for cost in grade["material_costs"]:
                assert cost["material_id"], "journal symbol required"
                assert cost["quantity"] >= 1
                assert cost["cost_status"] in {"verified", "unknown"}
                if cost["cost_status"] == "verified":
                    # provenance: resolved against the journal material index
                    assert cost["material_id"] != cost["source_name"] or cost["material_localised"]


def test_unknown_costs_are_never_zero(catalog: dict):
    """Contract rule: an absent/unknown cost is never interpreted as zero.

    A cost entry marked unknown must keep its source name so the planner can
    surface it as unverified instead of silently planning around it."""
    for blueprint in catalog["engineering_blueprints"]:
        for grade in blueprint["grades"].values():
            for cost in grade["material_costs"]:
                if cost["cost_status"] == "unknown":
                    assert cost["quantity"] >= 1
                    assert cost["source_name"], "unknown costs keep their display name"


def test_journal_blueprints_match_names_present(catalog: dict):
    """Blueprint ids follow the journal BlueprintName convention
    (Family_Effect) so _match_steps and the planner can join by fdname."""
    ids = {blueprint["id"] for blueprint in catalog["engineering_blueprints"]}
    for expected in ("FSD_LongRange", "ShieldGenerator_Thermic", "Weapon_LongRange",
                     "Engine_Dirty", "Engine_Tuned", "PowerPlant_Armoured"):
        assert expected in ids, f"missing journal blueprint {expected}"


def test_report_counts_are_consistent(catalog: dict, report: dict):
    assert report["counts"]["catalog_blueprints"] == len(catalog["engineering_blueprints"])
    assert report["counts"]["unknown_material_names"] == 0, (
        "all Coriolis materials must resolve to journal symbols"
    )
