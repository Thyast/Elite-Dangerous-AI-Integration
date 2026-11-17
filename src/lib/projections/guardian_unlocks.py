import json
from functools import lru_cache
from pathlib import Path
from dataclasses import dataclass

from typing_extensions import override
from pydantic import BaseModel, Field

from ..Database import KeyValueStore
from ..Event import Event, GameEvent
from ..EventManager import Projection
from ..Logger import log
from .common import get_state_dict


_SIZE_NAMES = {"1": "small", "2": "medium", "3": "large", "4": "huge"}
_CATALOG = Path(__file__).resolve().parents[2] / "assets" / "outfitting_catalog.json"
_GUARDIAN_ENTITLEMENT_PREFIX = "ELITE_HORIZONS_V_GUARDIAN_"

_override_store: KeyValueStore | None = None


class GuardianUnlocksStateModel(BaseModel):
    """Guardian unlock units recorded from TechnologyBroker journal events."""
    event: str = "GuardianUnlocks"
    timestamp: str = Field(default="1970-01-01T00:00:00Z", description="Last update timestamp")
    Unlocked: dict[str, str] = Field(default_factory=dict, description="Unlock unit key mapped to a display name")
    Symbols: dict[str, list[str]] = Field(default_factory=dict, description="Original journal symbols per unlock")
    Unrecognized: dict[str, str] = Field(default_factory=dict, description="Original journal symbol mapped to the reason it was not recognized")


@dataclass
class GuardianCatalogIndex:
    aliases: dict[str, str]
    labels: dict[str, str]
    symbols: dict[str, str]
    legacy_keys: dict[str, str]
    catalog_symbols: set[str]


@lru_cache(maxsize=1)
def _catalog_keys() -> GuardianCatalogIndex:
    catalog = json.loads(_CATALOG.read_text(encoding="utf-8"))
    modules = catalog["modules"]
    if not modules:
        raise ValueError("Outfitting module catalog is empty; run tools/import_catalog.py")
    keys: dict[str, str] = {}
    labels: dict[str, str] = {}
    symbols: dict[str, str] = {}
    legacy_keys: dict[str, str] = {}
    catalog_symbols = {row["symbol"].casefold() for row in modules}
    for row in modules:
        if "guardian" not in row["name"].casefold() or row["category"] not in ("internal", "hardpoint"):
            continue
        symbol = row["symbol"].casefold()
        entitlement = row["entitlement"]
        if not entitlement.startswith(_GUARDIAN_ENTITLEMENT_PREFIX):
            continue
        if row["category"] == "internal":
            label = row["name"]
            aliases = (symbol, label.casefold(), f"{label} size {row['class']}".casefold())
            legacy = row["symbol"].split("_Size", 1)[0].removeprefix("Int_").casefold()
        else:
            size = _SIZE_NAMES.get(row["class"])
            mount = "turret" if row["mount"] == "Turreted" else row["mount"].lower()
            if not size or not mount:
                raise ValueError(f"Invalid Guardian weapon in outfitting catalog: {row}")
            label = f"{row['name']} {size.capitalize()} {mount.capitalize()}"
            aliases = (symbol, label.casefold())
            legacy = f"{''.join(row['name'].casefold().split())}:{mount}:{size}"
        for alias in aliases:
            if alias in keys and keys[alias] != entitlement:
                raise ValueError(f"Ambiguous Guardian module alias: {alias}")
            keys[alias] = entitlement
        if entitlement in labels and labels[entitlement] != label:
            raise ValueError(f"Ambiguous Guardian entitlement: {entitlement}")
        labels[entitlement] = label
        symbols[symbol] = entitlement
        legacy_keys[legacy] = entitlement
        if row["category"] == "internal":
            legacy_keys["".join(row["name"].casefold().split())] = entitlement
    if not keys:
        raise ValueError("No Guardian modules in outfitting catalog; run tools/import_catalog.py")
    return GuardianCatalogIndex(keys, labels, symbols, legacy_keys, catalog_symbols)


def unlock_key(name: str) -> str | None:
    """Look up an exact catalog symbol or display variant, case-insensitively."""
    return _catalog_keys().aliases.get(name.casefold())


def display_label(key: str, fallback: str) -> str:
    return _catalog_keys().labels.get(key, fallback)


def resolve_unlocked(journal_unlocked: dict[str, str], overrides: dict[str, bool]) -> dict[str, str]:
    """Journal data always wins. A manual override only applies when the journal has no unlock for that unit."""
    resolved = dict(journal_unlocked)
    index = _catalog_keys()
    # Older manual overrides used derived family/size keys; prefer the new entitlement key on conflicts.
    legacy = {index.legacy_keys[key]: value for key, value in overrides.items()
              if key in index.legacy_keys}
    current = {key: value for key, value in overrides.items() if key in index.labels}
    effective = legacy | current
    for key, unlocked in effective.items():
        if unlocked and key not in resolved:
            resolved[key] = index.labels[key]
    return resolved


def valid_module_names() -> str:
    return ", ".join(sorted(_catalog_keys().labels.values()))


def get_override_store() -> KeyValueStore:
    global _override_store
    if _override_store is None:
        _override_store = KeyValueStore("guardian_unlock_overrides")
    return _override_store


def get_unlocked_guardian_modules(projected_states: dict, overrides: dict[str, bool] | None = None) -> list[str]:
    journal_unlocked = get_state_dict(projected_states, "GuardianUnlocks").get("Unlocked", {})
    if overrides is None:
        overrides = get_override_store().get_all()
    return sorted(resolve_unlocked(journal_unlocked, overrides).values())


def get_unrecognized_guardian_modules(projected_states: dict) -> dict[str, str]:
    return get_state_dict(projected_states, "GuardianUnlocks").get("Unrecognized", {})


class GuardianUnlocks(Projection[GuardianUnlocksStateModel]):
    StateModel = GuardianUnlocksStateModel

    @override
    def process(self, event: Event) -> None:
        if not isinstance(event, GameEvent) or event.content.get("event") != "TechnologyBroker":
            return
        if event.content.get("BrokerType") != "guardian":
            return

        items = event.content.get("ItemsUnlocked")
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                raw_name = item.get("Name")
                if not isinstance(raw_name, str) or not raw_name:
                    continue
                index = _catalog_keys()
                key = index.symbols.get(raw_name.casefold())
                if key is None:
                    reason = ("no standard Guardian entitlement" if raw_name.casefold() in index.catalog_symbols
                              else "not in outfitting catalog")
                    self.state.Unrecognized[raw_name] = reason
                    log("warn", f"Guardian broker module not recognized ({reason}): {raw_name}")
                    continue
                self.state.Unlocked.setdefault(key, display_label(key, raw_name))
                recorded = self.state.Symbols.setdefault(key, [])
                if raw_name not in recorded:
                    recorded.append(raw_name)

        timestamp = event.content.get("timestamp")
        if isinstance(timestamp, str):
            self.state.timestamp = timestamp
