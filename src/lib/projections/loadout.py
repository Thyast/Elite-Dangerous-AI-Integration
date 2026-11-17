from typing import Any, cast

from typing_extensions import override

from ..Event import Event, GameEvent
from ..EventManager import Projection
from pydantic import BaseModel, Field

from ..EventModels import HullDamageEvent, LoadoutEvent, RepairAllEvent


class LoadoutModifierA(BaseModel):
    LessIsGood: int
    OriginalValue: float
    Value: float
    Label: str

class LoadoutModifierB(BaseModel):
    ValueStr: str
    ValueStr_Localised: str
    Label: str


class LoadoutEngineering(BaseModel):
    Level: int | None = None
    EngineerID: int | None = None
    Engineer: str | None = None
    ExperimentalEffect: str | None = None
    BlueprintID: int | None = None
    Quality: float | None = None
    Modifiers: list[LoadoutModifierA | LoadoutModifierB] = Field(default_factory=list)
    ExperimentalEffect_Localised: str | None = None
    BlueprintName: str


class LoadoutModule(BaseModel):
    AmmoInHopper: int | None = None
    AmmoInClip: int | None = None
    Item: str
    Value: int | None = None
    Health: float | None = None
    Engineering: LoadoutEngineering | None = None
    Slot: str
    On: bool | None = None
    Priority: int | None = None


class LoadoutFuelCapacity(BaseModel):
    Reserve: float | None = None
    Main: float | None = None


class LoadoutState(BaseModel):
    """Current ship loadout including modules, fuel capacity, hull, and rebuy value."""
    ShipName: str | None = None
    HullValue: int | None = None
    HullHealth: float | None = None
    ShipIdent: str | None = None
    ModulesValue: int | None = None
    CargoCapacity: int | None = None
    Modules: list[LoadoutModule] = Field(default_factory=list)
    FuelCapacity: LoadoutFuelCapacity | None = None
    MaxJumpRange: float | None = None
    Rebuy: int | None = None
    ShipID: int | None = None
    event: str | None = None
    timestamp: str | None = None
    UnladenMass: float | None = None
    Ship: str | None = None
    Hot: bool | None = None


class Loadout(Projection[LoadoutState]):
    """Track fitted modules and per-slot engineering across journal replay."""

    StateModel = LoadoutState

    @override
    def process(self, event: Event) -> None:
        if isinstance(event, GameEvent) and event.content.get("event") == "Loadout":
            payload = cast(LoadoutEvent, event.content)
            self.state.ShipName = payload.get("ShipName")
            self.state.HullValue = payload.get("HullValue")
            self.state.HullHealth = payload.get("HullHealth")
            self.state.ShipIdent = payload.get("ShipIdent")
            self.state.ModulesValue = payload.get("ModulesValue")
            self.state.CargoCapacity = payload.get("CargoCapacity")
            self.state.Modules = []
            for module in payload.get("Modules", []):
                engineering = module.get("Engineering")
                if isinstance(engineering, dict):
                    modifiers: list[LoadoutModifierA | LoadoutModifierB] = []
                    for mod in engineering.get("Modifiers", []):
                        if "LessIsGood" in mod:
                            modifiers.append(LoadoutModifierA(**mod))
                        elif "ValueStr" in mod:
                            modifiers.append(LoadoutModifierB(**mod))
                    engineering_data = {**engineering, "Modifiers": modifiers}
                    engineering_model = LoadoutEngineering(**engineering_data)
                else:
                    engineering_model = None
                self.state.Modules.append(
                    LoadoutModule(
                        AmmoInHopper=module.get("AmmoInHopper"),
                        AmmoInClip=module.get("AmmoInClip"),
                        Item=module.get("Item", ""),
                        Value=module.get("Value"),
                        Health=module.get("Health", 0.0),
                        Engineering=engineering_model,
                        Slot=module.get("Slot", ""),
                        On=module.get("On", False),
                        Priority=module.get("Priority", 0),
                    )
                )
            fuel_capacity = payload.get("FuelCapacity")
            self.state.FuelCapacity = LoadoutFuelCapacity(**fuel_capacity) if isinstance(fuel_capacity, dict) else None
            self.state.MaxJumpRange = payload.get("MaxJumpRange")
            self.state.Rebuy = payload.get("Rebuy")
            self.state.ShipID = payload.get("ShipID")
            self.state.event = payload.get("event")
            self.state.timestamp = payload.get("timestamp")
            self.state.UnladenMass = payload.get("UnladenMass")
            self.state.Ship = payload.get("Ship")
            self.state.Hot = payload.get("Hot")
            return

        if isinstance(event, GameEvent) and event.content.get("event") in {
            "ModuleBuy", "ModuleRetrieve", "ModuleStore", "ModuleSell",
            "MassModuleStore", "ModuleSwap", "EngineerCraft",
        }:
            change = cast(dict[str, Any], event.content)
            ship_id = change.get("ShipID", self.state.ShipID)
            if self.state.ShipID is not None and str(ship_id) == str(self.state.ShipID):
                if self._update_modules(change):
                    self.state.timestamp = change.get("timestamp", self.state.timestamp)
            return

        if isinstance(event, GameEvent) and event.content.get("event") == "HullDamage":
            payload = cast(HullDamageEvent, event.content)
            if payload.get("Fighter") is not True:
                self.state.HullHealth = payload.get("Health")
            return

        if isinstance(event, GameEvent) and event.content.get("event") in ["RepairAll", "Died"]:
            _payload = cast(RepairAllEvent, event.content)
            self.state.HullHealth = 1.0

    @staticmethod
    def _item(value: Any) -> str:
        return str(value or "").strip().lower().removeprefix("$").removesuffix("_name;")

    def _update_modules(self, change: dict[str, Any]) -> bool:
        event_name = change["event"]
        slot = str(change.get("Slot") or "").casefold()
        if event_name == "ModuleSwap":
            first = str(change.get("FromSlot") or "").casefold()
            second = str(change.get("ToSlot") or "").casefold()
            if not first or not second:
                return False
            self.state.Modules = [
                module.model_copy(update={"Slot": change["ToSlot"]})
                if module.Slot.casefold() == first else
                module.model_copy(update={"Slot": change["FromSlot"]})
                if module.Slot.casefold() == second else module
                for module in self.state.Modules
            ]
            return True
        if event_name == "MassModuleStore":
            slots = {str(item.get("Slot") or "").casefold() for item in change.get("Items", [])}
            self.state.Modules = [module for module in self.state.Modules if module.Slot.casefold() not in slots]
            return True
        if not slot:
            return False
        if event_name == "EngineerCraft":
            item = self._item(change.get("Module"))
            for index, module in enumerate(self.state.Modules):
                if module.Slot.casefold() != slot or (item and self._item(module.Item) != item):
                    continue
                engineering = {
                    key: change[key] for key in LoadoutEngineering.model_fields if key in change
                }
                if change.get("ApplyExperimentalEffect"):
                    engineering["ExperimentalEffect"] = change["ApplyExperimentalEffect"]
                previous = module.Engineering
                if (previous is not None
                        and previous.BlueprintName.casefold() == str(engineering.get("BlueprintName") or "").casefold()
                        and not engineering.get("ExperimentalEffect")):
                    engineering["ExperimentalEffect"] = previous.ExperimentalEffect
                    engineering["ExperimentalEffect_Localised"] = previous.ExperimentalEffect_Localised
                updated = LoadoutEngineering.model_validate(engineering)
                self.state.Modules[index] = module.model_copy(update={"Engineering": updated})
                return True
            return False
        if event_name in {"ModuleStore", "ModuleSell"}:
            item = self._item(change.get("ReplacementItem"))
            engineering = None
        else:
            item = self._item(change.get("BuyItem" if event_name == "ModuleBuy" else "RetrievedItem"))
            if not item or item == "null":
                return False
            blueprint = change.get("EngineerModifications")
            engineering = LoadoutEngineering(
                BlueprintName=blueprint, Level=change.get("Level"), Quality=change.get("Quality"),
            ) if isinstance(blueprint, str) and blueprint else None
        self.state.Modules = [module for module in self.state.Modules if module.Slot.casefold() != slot]
        if item and item != "null":
            self.state.Modules.append(LoadoutModule(
                Slot=change["Slot"], Item=item, Engineering=engineering,
            ))
        return True
