from typing import Any
import json
from pathlib import Path

import pytest

from src.lib.Event import GameEvent
from src.lib.actions import actions_guardian
from src.lib.actions.actions_guardian import set_guardian_unlock
from src.lib.projections import guardian_unlocks
from src.lib.projections.guardian_unlocks import (
    GuardianUnlocks,
    GuardianUnlocksStateModel,
    get_unlocked_guardian_modules,
    get_unrecognized_guardian_modules,
    resolve_unlocked,
    unlock_key,
)


def game_event(content: dict[str, object]) -> GameEvent:
    return GameEvent(content=content, historic=False)


def broker_event(broker_type: str, items: list[dict[str, Any]]) -> GameEvent:
    return game_event({
        "timestamp": "2026-07-12T20:00:00Z",
        "event": "TechnologyBroker",
        "BrokerType": broker_type,
        "ItemsUnlocked": items,
    })


class FakeOverrideStore:
    def __init__(self, initial: dict[str, bool] | None = None) -> None:
        self.data: dict[str, bool] = dict(initial or {})

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value

    def get_all(self) -> dict[str, bool]:
        return dict(self.data)

    def delete(self, key: str) -> None:
        self.data.pop(key, None)


def test_projection_records_guardian_unlocks_and_dedupes_sizes() -> None:
    projection = GuardianUnlocks()

    projection.process(broker_event("guardian", [
        {"Name": "Int_GuardianFSDBooster_Size1", "Name_Localised": "$Int_GuardianFSDBooster_Size1_Name;"},
        {"Name": "Int_GuardianFSDBooster_Size2"},
        {"Name": "Hpt_Guardian_GaussCannon_Fixed_Medium", "Name_Localised": "Guardian Gauss Cannon"},
    ]))

    assert projection.state.Unlocked == {
        "ELITE_HORIZONS_V_GUARDIAN_FSDBOOSTER": "Guardian FSD Booster",
        "ELITE_HORIZONS_V_GUARDIAN_GAUSS_FIXED_MEDIUM": "Guardian Gauss Cannon Medium Fixed",
    }
    assert projection.state.Symbols["ELITE_HORIZONS_V_GUARDIAN_FSDBOOSTER"] == [
        "Int_GuardianFSDBooster_Size1", "Int_GuardianFSDBooster_Size2"
    ]
    assert projection.state.timestamp == "2026-07-12T20:00:00Z"


def test_projection_ignores_human_broker_events() -> None:
    projection = GuardianUnlocks()

    projection.process(broker_event("human", [{"Name": "Int_GuardianFSDBooster_Size1"}]))

    assert projection.state.Unlocked == {}
    assert projection.state.Unrecognized == {}


def test_projection_does_not_record_non_catalog_symbol(monkeypatch: pytest.MonkeyPatch) -> None:
    warnings = []
    monkeypatch.setattr(guardian_unlocks, "log", lambda *args: warnings.append(args))
    projection = GuardianUnlocks()

    projection.process(broker_event("guardian", [{"Name": "Hpt_GuardianGaussCannon_Fixed_Medium"}]))

    assert projection.state.Unlocked == {}
    assert projection.state.Unrecognized == {
        "Hpt_GuardianGaussCannon_Fixed_Medium": "not in outfitting catalog"
    }
    assert warnings == [(
        "warn",
        "Guardian broker module not recognized (not in outfitting catalog): Hpt_GuardianGaussCannon_Fixed_Medium",
    )]


def test_unit_key_uses_entitlement_for_internal_families_and_weapon_variants() -> None:
    assert unlock_key("Int_GuardianPowerplant_Size2") == "ELITE_HORIZONS_V_GUARDIAN_POWERPLANT"
    assert unlock_key("Int_GuardianFSDBooster_Size5") == unlock_key("Int_GuardianFSDBooster_Size1")
    assert unlock_key("Hpt_Guardian_PlasmaLauncher_Turret_Large") == "ELITE_HORIZONS_V_GUARDIAN_PLASMA_TURRET_LARGE"
    assert unlock_key("hpt_guardian_plasmalauncher_turret_large") == "ELITE_HORIZONS_V_GUARDIAN_PLASMA_TURRET_LARGE"
    assert unlock_key("Guardian Plasma Charger Medium Turret") == "ELITE_HORIZONS_V_GUARDIAN_PLASMA_TURRET_MEDIUM"
    assert unlock_key("GUARDIAN PLASMA CHARGER MEDIUM TURRET") == "ELITE_HORIZONS_V_GUARDIAN_PLASMA_TURRET_MEDIUM"
    assert unlock_key("Guardian Plasma Charger Turret Medium") is None
    assert unlock_key("Guardian Plasma Launcher Medium Turret") is None
    assert unlock_key("Hpt_GuardianPlasmaLauncher_Turret_Large") is None
    assert unlock_key("Hpt_Guardian_GaussCannon_Fixed_Medium") == "ELITE_HORIZONS_V_GUARDIAN_GAUSS_FIXED_MEDIUM"
    assert unlock_key("Hpt_GuardianGaussCannon_Fixed_Medium") is None
    assert unlock_key("Guardian Plasma Charger Medium Turreted") is None
    assert unlock_key("Hpt_Guardian_ShardCannon_Turret_Small") != unlock_key("Hpt_Guardian_ShardCannon_Fixed_Small")
    assert unlock_key("Hpt_ATVentDisruptorPylon_Fixed_Large") is None
    assert unlock_key("Hpt_Guardian_ShardCannon_Gimbal_Small") is None
    assert unlock_key("Guardian Gauss Cannon Fixed Medium") is None
    assert unlock_key("Guardian Gauss Cannon Large Fixed") is None
    assert unlock_key("Hpt_SomethingElse_Fixed_Small") is None


def test_every_standard_guardian_module_uses_catalog_entitlement() -> None:
    catalog = json.loads((Path(__file__).parents[3] / "src/assets/outfitting_catalog.json").read_text(encoding="utf-8"))
    modules = [row for row in catalog["modules"] if "Guardian" in row["name"]]
    standard = [row for row in modules if row["entitlement"].startswith("ELITE_HORIZONS_V_GUARDIAN_")]
    assert len(standard) == 64
    assert all(unlock_key(row["symbol"]) == row["entitlement"] for row in standard)
    assert all(unlock_key(row["symbol"]) is None for row in modules if row not in standard)
    for family in ("HullReinforcement", "ModuleReinforcement", "ShieldReinforcement"):
        variants = [row for row in standard if row["symbol"].startswith(f"Int_Guardian{family}_")]
        assert len(variants) == 10
        assert {row["class"] for row in variants} == set("12345")
        assert {row["rating"] for row in variants} == {"D", "E"}
        assert len({row["entitlement"] for row in variants}) == 1


def test_real_broker_fsd_unlock_journal_fields_group_all_sizes() -> None:
    # Sanitized from Journal.210110073206.01.log; omit account/station identifiers.
    projection = GuardianUnlocks()
    projection.process(game_event({
        "timestamp": "2021-01-10T07:22:59Z",
        "event": "TechnologyBroker",
        "BrokerType": "guardian",
        "ItemsUnlocked": [
            {"Name": "Int_GuardianFSDBooster_Size1", "Name_Localised": "Guardian FSD Booster"},
            *({"Name": f"Int_GuardianFSDBooster_Size{n}",
                "Name_Localised": "$Int_GuardianFSDBooster_Size1_Name;"} for n in range(2, 6)),
        ],
    }))
    assert projection.state.Unlocked == {"ELITE_HORIZONS_V_GUARDIAN_FSDBOOSTER": "Guardian FSD Booster"}
    assert len(projection.state.Symbols["ELITE_HORIZONS_V_GUARDIAN_FSDBOOSTER"]) == 5
    assert projection.state.Unrecognized == {}


def test_distinct_weapon_entitlements_and_shared_internal_entitlements() -> None:
    projection = GuardianUnlocks()
    projection.process(broker_event("guardian", [
        {"Name": "Int_GuardianHullReinforcement_Size1_Class1"},
        {"Name": "Int_GuardianHullReinforcement_Size5_Class2"},
        {"Name": "Int_GuardianPowerDistributor_Size1"},
        {"Name": "Int_GuardianPowerDistributor_Size8"},
        {"Name": "Hpt_Guardian_PlasmaLauncher_Fixed_Medium"},
        {"Name": "Hpt_Guardian_PlasmaLauncher_Turret_Medium"},
        {"Name": "Hpt_Guardian_GaussCannon_Fixed_Small"},
        {"Name": "Hpt_Guardian_GaussCannon_Fixed_Medium"},
    ]))
    assert set(projection.state.Unlocked) == {
        "ELITE_HORIZONS_V_GUARDIAN_HULLREINFORCEMENT",
        "ELITE_HORIZONS_V_GUARDIAN_POWERDISTRIBUTOR",
        "ELITE_HORIZONS_V_GUARDIAN_PLASMA_FIXED_MEDIUM",
        "ELITE_HORIZONS_V_GUARDIAN_PLASMA_TURRET_MEDIUM",
        "ELITE_HORIZONS_V_GUARDIAN_GAUSS_FIXED_SMALL",
        "ELITE_HORIZONS_V_GUARDIAN_GAUSS_FIXED_MEDIUM",
    }


def test_catalog_module_without_entitlement_is_unrecognized(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(guardian_unlocks, "log", lambda *args: None)
    projection = GuardianUnlocks()
    projection.process(broker_event("guardian", [{"Name": "Hpt_ATVentDisruptorPylon_Fixed_Large"}]))
    assert projection.state.Unlocked == {}
    assert get_unrecognized_guardian_modules({"GuardianUnlocks": projection.state}) == {
        "Hpt_ATVentDisruptorPylon_Fixed_Large": "no standard Guardian entitlement"
    }


def test_journal_unlock_wins_and_override_only_applies_when_journal_is_silent() -> None:
    journal = {"ELITE_HORIZONS_V_GUARDIAN_FSDBOOSTER": "Guardian FSD Booster"}

    resolved = resolve_unlocked(journal, {"ELITE_HORIZONS_V_GUARDIAN_FSDBOOSTER": False,
                                           "ELITE_HORIZONS_V_GUARDIAN_GAUSS_FIXED_SMALL": True})

    assert resolved == {
        "ELITE_HORIZONS_V_GUARDIAN_FSDBOOSTER": "Guardian FSD Booster",
        "ELITE_HORIZONS_V_GUARDIAN_GAUSS_FIXED_SMALL": "Guardian Gauss Cannon Small Fixed",
    }


def test_false_override_does_not_remove_journal_unlock() -> None:
    journal = {"ELITE_HORIZONS_V_GUARDIAN_FSDBOOSTER": "Guardian FSD Booster"}

    assert resolve_unlocked(journal, {"guardianfsdbooster": False}) == journal


def test_get_unlocked_guardian_modules_merges_journal_and_overrides() -> None:
    states = {"GuardianUnlocks": GuardianUnlocksStateModel(
        Unlocked={"ELITE_HORIZONS_V_GUARDIAN_FSDBOOSTER": "Guardian FSD Booster"}
    )}

    assert get_unlocked_guardian_modules(states, {"guardianhullreinforcement": True}) == [
        "Guardian FSD Booster",
        "Guardian Hull Reinforcement",
    ]


def test_set_action_marks_module_unlocked_and_locked(monkeypatch: pytest.MonkeyPatch) -> None:
    store = FakeOverrideStore()
    monkeypatch.setattr(actions_guardian, "get_override_store", lambda: store)

    result = set_guardian_unlock({"module": "Guardian Shield Reinforcement", "unlocked": True}, {})
    assert "now marked as unlocked" in result
    assert store.data == {"ELITE_HORIZONS_V_GUARDIAN_SHIELDREINFORCEMENT": True}

    result = set_guardian_unlock({"module": "Guardian Shield Reinforcement", "unlocked": False}, {})
    assert "now marked as locked" in result
    assert store.data == {"ELITE_HORIZONS_V_GUARDIAN_SHIELDREINFORCEMENT": False}

    result = set_guardian_unlock({"module": "guardian plasma charger medium turret", "unlocked": True}, {})
    assert "Guardian Plasma Charger Medium Turret is now marked as unlocked." == result
    assert store.data["ELITE_HORIZONS_V_GUARDIAN_PLASMA_TURRET_MEDIUM"] is True

    result = set_guardian_unlock({"module": "Guardian Plasma Charger Turret Medium", "unlocked": True}, {})
    assert result.startswith("ERROR:")


def test_set_action_reports_journal_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    store = FakeOverrideStore()
    monkeypatch.setattr(actions_guardian, "get_override_store", lambda: store)
    states = {"GuardianUnlocks": GuardianUnlocksStateModel(
        Unlocked={"ELITE_HORIZONS_V_GUARDIAN_FSDBOOSTER": "Guardian FSD Booster"}
    )}

    result = set_guardian_unlock({"module": "Guardian FSD Booster", "unlocked": False}, states)

    assert "journal already records it as unlocked" in result
    assert store.data == {"ELITE_HORIZONS_V_GUARDIAN_FSDBOOSTER": False}


def test_manual_legacy_override_migrates_when_updated(monkeypatch: pytest.MonkeyPatch) -> None:
    store = FakeOverrideStore({"guardianfsdbooster": True, "guardianhybridpowerplant": True})
    monkeypatch.setattr(actions_guardian, "get_override_store", lambda: store)
    assert resolve_unlocked({}, store.get_all()) == {
        "ELITE_HORIZONS_V_GUARDIAN_FSDBOOSTER": "Guardian FSD Booster",
        "ELITE_HORIZONS_V_GUARDIAN_POWERPLANT": "Guardian Hybrid Power Plant",
    }
    set_guardian_unlock({"module": "Guardian FSD Booster", "unlocked": False}, {})
    assert store.data == {"ELITE_HORIZONS_V_GUARDIAN_FSDBOOSTER": False, "guardianhybridpowerplant": True}
    assert resolve_unlocked({}, store.get_all()) == {
        "ELITE_HORIZONS_V_GUARDIAN_POWERPLANT": "Guardian Hybrid Power Plant"
    }


def test_set_action_rejects_unknown_module_and_bad_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    store = FakeOverrideStore()
    monkeypatch.setattr(actions_guardian, "get_override_store", lambda: store)

    assert set_guardian_unlock({"module": "Bogus Module", "unlocked": True}, {}).startswith("ERROR:")
    assert set_guardian_unlock({"module": 42, "unlocked": True}, {}).startswith("ERROR:")
    assert set_guardian_unlock({"module": "Guardian FSD Booster", "unlocked": "yes"}, {}).startswith("ERROR:")
    assert store.data == {}
