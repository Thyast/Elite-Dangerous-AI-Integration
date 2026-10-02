import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, cast, override

from pydantic import BaseModel, Field

from lib.Event import Event, GameEvent, PluginEvent, ProjectedEvent
from lib.EventManager import Projection
from lib.Logger import log
from lib.Config import Config, get_asset_path, get_ed_journals_path
from lib.PluginBase import PluginBase, PluginManifest
from lib.PluginHelper import PluginHelper
from lib.PluginSettingDefinitions import (
    ButtonSetting,
    FilterSetting,
    ListRow,
    ListRowProgress,
    ListSetting,
    ParagraphSetting,
    PluginSettings,
    SettingField,
    SettingsGrid,
    TextAreaSetting,
)
from .parsers import PlanParseError, parse_plan_input
from .planner import (
    CLASS_LETTERS,
    PlannedAction,
    PlannedTrade,
    SlotDelta,
    _engineers_for_blueprint,
    analyze_build,
    build_advice,
    catalog_entry,
    canonical_item,
    engineering_increments,
    grade_reached,
)
from .materials import (
    MaterialReference,
    StepNeed,
    acquisition_plan,
    aggregate_needs,
    inventory_from_state,
    material_status,
    shortfalls,
    step_need,
    trade_options,
)

if TYPE_CHECKING:
    from lib.PluginManager import PluginManager


PLUGIN_GUID = "f1d78e6b-3e3b-4dc6-a61c-bff3e2b2f11e"

IMPORT_STATE_IDLE = "idle"
IMPORT_STATE_DATA = "data"
IMPORT_STATE_IMPORTED = "imported"

LAST_IMPORT_META_KEY = "last_import"


def _load_ship_names() -> dict[str, str]:
    """Public display names for internal ship identifiers (see ship_names.json)."""
    try:
        with open(get_asset_path("ship_names.json"), encoding="utf-8") as handle:
            return {
                key: value
                for key, value in json.load(handle).items()
                if not key.startswith("_")
            }
    except (OSError, json.JSONDecodeError):
        return {}


SHIP_NAMES = _load_ship_names()


def _load_module_families() -> list[tuple[str, str]]:
    """Pretty module family names from the engineering catalogue, longest match first."""
    try:
        with open(
            get_asset_path("engineering_modifications.json"), encoding="utf-8"
        ) as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return []
    families: dict[str, str] = {}
    for blueprint in data.values():
        if not isinstance(blueprint, dict):
            continue
        for family in blueprint.get("module_recipes") or {}:
            key = re.sub(r"[^a-z0-9]", "", str(family).lower())
            families.setdefault(key, str(family))
    return sorted(families.items(), key=lambda pair: -len(pair[0]))


MODULE_FAMILIES = _load_module_families()

CORE_MODULE_FAMILIES = {
    "powerplant",
    "engine",
    "hyperdrive",
    "lifesupport",
    "powerdistributor",
    "radar",
    "fueltank",
}

# Standard core internal slot names — checked before the item family so
# ship-specific variants (MkIIAgileBoost engine, armour) classify correctly.
CORE_SLOTS = {
    "armour",
    "powerplant",
    "mainengines",
    "frameshiftdrive",
    "lifesupport",
    "powerdistributor",
    "radar",
    "fueltank",
}

UTILITY_MODULE_FAMILIES = {
    "shieldbooster",
    "heatsinklauncher",
    "chafflauncher",
    "electroniccountermeasure",
    "cargoscanner",
    "manifestscanner",
    "killwarrantscanner",
    "cloudscanner",
}

CATEGORY_ORDER = [
    "plugin.sum.category.coreInternal",
    "plugin.sum.category.optionalInternal",
    "plugin.sum.category.hardpoints",
    "plugin.sum.category.utilityMounts",
]


def _blueprint_family_display(blueprint: str) -> str:
    """'ShieldGenerator_Reinforced' -> 'Reinforced'."""
    return re.sub(r"(?<!^)(?=[A-Z])", " ", blueprint.split("_")[-1])


@lru_cache(maxsize=2)
def _engineering_catalog_names(filename: str, id_field: str) -> dict[str, str]:
    catalog_path = Path(__file__).resolve().parent / "assets" / filename
    try:
        with catalog_path.open(encoding="utf-8") as handle:
            catalog = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Unable to load Ship Upgrade Manager engineering catalog: {catalog_path}"
        ) from exc
    if not isinstance(catalog, dict):
        raise ValueError(f"Invalid engineering catalog format: {catalog_path}")

    names: dict[str, str] = {}
    for catalog_id, entry in catalog.items():
        if not isinstance(entry, dict):
            raise ValueError(
                f"Invalid entry {catalog_id!r} in engineering catalog: {catalog_path}"
            )
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(
                f"Missing display name for {catalog_id!r} in engineering catalog: "
                f"{catalog_path}"
            )
        for identifier in (catalog_id, entry.get(id_field)):
            if isinstance(identifier, str) and identifier.strip():
                names[identifier.strip()] = name.strip()
    return names


def _engineering_catalog_display_name(
    identifier: str, filename: str, id_field: str
) -> str:
    identifier = identifier.strip()
    if not identifier:
        return ""
    names = _engineering_catalog_names(filename, id_field)
    return names.get(identifier) or next(
        (name for key, name in names.items() if key.casefold() == identifier.casefold()),
        identifier,
    )


@lru_cache(maxsize=1)
def _specials_catalog() -> dict[str, Any]:
    """Experimental effects with their material recipe (assets/specials.json)."""
    catalog_path = Path(__file__).resolve().parent / "assets" / "specials.json"
    try:
        with catalog_path.open(encoding="utf-8") as handle:
            catalog = json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        log("warn", f"Ship Upgrade Manager specials catalog load failed: {error}")
        return {}
    return catalog if isinstance(catalog, dict) else {}


@lru_cache(maxsize=1)
def _material_reference() -> MaterialReference:
    """Material names, grades, families and sources shared with material_finder."""
    from lib.actions.data import (
        display_names,
        ship_encoded_materials_map,
        ship_manufactured_materials_map,
        ship_raw_materials_map,
    )

    with open(get_asset_path("materials.json"), encoding="utf-8") as handle:
        template = json.load(handle)
    return MaterialReference(
        template,
        display_names,
        {
            "Raw": ship_raw_materials_map,
            "Manufactured": ship_manufactured_materials_map,
            "Encoded": ship_encoded_materials_map,
        },
    )


MATERIALS_UNAVAILABLE = (
    "Your material inventory has not been reported by the game yet, so "
    "stock cannot be compared with the upgrade needs."
)


# Up to this many missing target modules are named directly; beyond it the
# player gets the count and the most important ones to install first.
MISSING_MODULES_NAMED = 3

# Install importance of missing modules: core internals (in this order), then
# the shield generator, weapons (largest mount first), optional internals
# (largest first) and finally utility mounts.
CORE_SLOT_ORDER = (
    "powerplant",
    "mainengines",
    "frameshiftdrive",
    "powerdistributor",
    "armour",
    "lifesupport",
    "radar",
    "fueltank",
)
CORE_FAMILY_SLOTS = {
    "powerplant": "powerplant",
    "engine": "mainengines",
    "hyperdrive": "frameshiftdrive",
    "powerdistributor": "powerdistributor",
    "lifesupport": "lifesupport",
    "sensors": "radar",
    "fueltank": "fueltank",
}
HARDPOINT_MOUNT_SIZES = {"huge": 4, "large": 3, "medium": 2, "small": 1}

# Spec module families (normalized) — the i18n keys of the UI module
# dictionary (module.<family>). Longest-prefix match wins over variants.
SPEC_MODULE_FAMILIES = [
    "shieldgeneratorstrong",
    "shieldgeneratorfast",
    "hyperdriveovercharge",
    "enginefast",
    "cargorackcorrosionresistant",
    "miningseismicchargelauncher",
    "miningsubsurfacedisplacement",
    "miningabrasionblaster",
    "guardiangausscannon",
    "guardianplasmalauncher",
    "guardianshardcannon",
    "guardianfsdbooster",
    "guardianhullreinforcement",
    "guardianshieldreinforcement",
    "thargoidempneutraliser",
    "dockingcomputeradvanced",
    "dockingcomputerstandard",
    "detailedsurfacescanner",
    "electroniccountermeasure",
    "plasmanavigationalbeacon",
    "pulsescandiscovery",
    "hyperdriveinterdictor",
    "atmulticannon",
    "atmissiletile",
    "basicmissilerack",
    "dumbfirermissilerack",
    "multicannonadvanced",
    "slugshotadvanced",
    "railgunburst",
    "dronecontrolcollection",
    "dronecontrolprospector",
    "dronecontrolfueltransfer",
    "dronecontrolrepair",
    "dronecontroldecontamination",
    "dronecontrolmultipurpose",
    "remotereleaseflak",
    "flakmortar",
    "mininglaser",
    "shieldcellbank",
    "passengercabin",
    "powerdistributor",
    "pulselaserburst",
    "plasmaaccelerator",
    "heatsinklauncher",
    "shieldbooster",
    "chafflauncher",
    "crimescanner",
    "cloudscanner",
    "cargoscanner",
    "xenoscanner",
    "torpedopylon",
    "minelauncher",
    "supercruiseassist",
    "hullreinforcement",
    "modulereinforcement",
    "multicannon",
    "powerplant",
    "lifesupport",
    "fuelscoop",
    "fighterbay",
    "buggybay",
    "refinery",
    "hyperdrive",
    "railgun",
    "slugshot",
    "sensors",
    "fueltank",
    "beamlaser",
    "pulselaser",
    "cannon",
    "engine",
    "mkiiplasmashockautocannon",
    "mkiiagileboostengine",
    "guardianmodulereinforcement",
    "smallcombat01nxarmourreactive",
    "modularcargobaydoor",
    "plasmashockautocannon",
    "engineagile",
]

def _spec_module_key(family_key: str) -> str:
    """Longest spec family that prefixes the id family, else the id family."""
    return next(
        (
            name
            for name in sorted(SPEC_MODULE_FAMILIES, key=len, reverse=True)
            if family_key.startswith(name)
        ),
        family_key,
    )


def _journal_files(config: Config | None) -> list[str]:
    """Journal.*.log files of the configured (ed_journal_path) or default
    Elite Dangerous journal folder."""
    # Without the host config, an empty one selects the default folder.
    journals_path = get_ed_journals_path(
        config if config is not None else cast(Config, cast(object, {}))
    )
    return [
        os.path.join(journals_path, name)
        for name in os.listdir(journals_path)
        if os.path.isfile(os.path.join(journals_path, name))
        and name.startswith("Journal.")
    ]


class _JournalWatcher(threading.Thread):
    """Tails the Elite journals while the runtime is not running.

    Journal side effects only reach the plugin after the user presses Run;
    this watcher lets the plugin track ship changes from C:N startup in
    config state. While the runtime runs, the EventManager delivers the
    same events and the watcher keeps tailing but drops the entries, so
    there is never double processing."""

    def __init__(self, plugin: "ShipUpgradeManagerPlugin"):
        super().__init__(daemon=True, name="ship-upgrade-journal-watcher")
        self.plugin: ShipUpgradeManagerPlugin = plugin
        self._path: str = ""
        self._offset: int = 0
        self._remainder: str = ""

    @override
    def run(self) -> None:
        while True:
            try:
                self._poll()
            except Exception as error:
                log("warn", f"Ship Upgrade Manager journal watcher error: {error}")
            time.sleep(1.0)

    def _poll(self) -> None:
        try:
            log_files = self.plugin.journal_files()
        except (OSError, FileNotFoundError):
            return
        latest = max(log_files, key=os.path.getmtime) if log_files else ""
        if not latest:
            return
        if latest != self._path:
            # Start at the end of the newest file: the session baseline
            # already reflects the historical state.
            self._path = latest
            self._offset = os.path.getsize(latest) if latest else 0
            self._remainder = ""
            return
        size = os.path.getsize(latest)
        if size < self._offset:
            self._offset = 0
            self._remainder = ""
        if size == self._offset:
            return
        with open(latest, "rb") as handle:
            handle.seek(self._offset)
            chunk = handle.read()
        self._offset += len(chunk)
        lines = (self._remainder + chunk.decode("utf-8", errors="ignore")).split("\n")
        self._remainder = lines.pop()
        for line in lines:
            self._dispatch(line)

    def _dispatch(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            return
        if isinstance(entry, dict):
            self.plugin.handle_watched_journal_entry(entry)


class ShipUpgradeState(BaseModel):
    plan_id: str | None = None
    plan_name: str | None = None
    ship_model: str | None = None
    ship_instance_id: str | None = None
    ship_custom_name: str | None = None
    current_step: int = 0
    completed_steps: list[str] = Field(default_factory=list)
    total_steps: int = 0
    paused: bool = False


class ShipUpgradeProjection(Projection[ShipUpgradeState]):
    StateModel: type[BaseModel] = ShipUpgradeState
    state: ShipUpgradeState

    @override
    def process(self, event: Event[Any]) -> None:
        if not isinstance(event, ProjectedEvent):
            return
        if event.content.get("projection") != "ship_upgrade_manager":
            return
        payload = event.content.get("state")
        if isinstance(payload, dict):
            self.state = ShipUpgradeState.model_validate(payload)


class StartSessionParams(BaseModel):
    plan_name: str = Field(description="Name of the ship upgrade plan to start")
    ship_instance_id: str = Field(description="Unique identifier of the current ship")
    ship_custom_name: str = Field(default="", description="Optional ship name")


class CompleteStepParams(BaseModel):
    step_id: str = Field(description="Identifier of the completed upgrade module")
    notes: str = Field(default="", description="Optional completion notes")
    session_id: str | None = Field(default=None, description="Optional session id")


class SessionActionParams(BaseModel):
    session_id: str | None = Field(default=None, description="Optional session id")


class GoalParams(BaseModel):
    plan_name: str = Field(description="Name of the plan to activate as the current build goal")


class StateQueryParams(BaseModel):
    ship: str | None = Field(default=None, description="Optional ship name or id to filter on")


class LoadoutQueryParams(BaseModel):
    slot: str | None = Field(
        default=None,
        description="Optional technical ship slot such as Armour, PowerPlant, or FrameShiftDrive",
    )


class BuildAdviceParams(BaseModel):
    session_id: str | None = Field(default=None, description="Optional active session id")
    module: str | None = Field(
        default=None,
        description=(
            "Optional specific module to plan for. Leave empty for the complete "
            "active/imported ship build route. Use when the player names one module."
        ),
    )
    detailed: bool = Field(default=False, description="List every stop, engineer, upgrade, effect and estimated jumps; use for a request to detail the full route")


class PlanChangeParams(BaseModel):
    plan_input: str = Field(description="JSON loadout or supported plan URL")


class MissingModulesParams(BaseModel):
    list_all: bool = Field(
        default=False,
        description=(
            "True only when the player explicitly asks for the complete list of "
            "missing modules; otherwise leave false."
        ),
    )
    session_id: str | None = Field(default=None, description="Optional session id")


class MissingMaterialsParams(BaseModel):
    count: int | None = Field(
        default=None,
        description=(
            "Number of most-missing materials to list with a collection plan, "
            "only when the player asks for a specific number. Leave empty when "
            "the player asks generically which materials are missing."
        ),
    )
    session_id: str | None = Field(default=None, description="Optional session id")


class MaterialQueryParams(BaseModel):
    material: str = Field(
        description="Engineering material named by the player, e.g. 'Proprietary Composites'"
    )
    session_id: str | None = Field(default=None, description="Optional session id")


class StepMaterialsParams(BaseModel):
    module: str | None = Field(
        default=None,
        description=(
            "Optional plan module to check. Leave empty to check the next upgrade."
        ),
    )
    session_id: str | None = Field(default=None, description="Optional session id")


class ShipUpgradeManagerPlugin(PluginBase):
    """Persist generic ship plans and per-ship upgrade sessions."""

    settings_schema_version: ClassVar[int] = 1

    def __init__(self, plugin_manifest: PluginManifest):
        super().__init__(plugin_manifest)
        self.helper: PluginHelper | None = None
        self._current_ship_id: str = ""
        self._current_ship_name: str = ""
        self._current_fid: str = ""
        self._current_position: tuple[float, float, float] | None = None
        self._current_system: str = ""
        self._current_workshop: str | None = None
        self._maximum_jump_range_by_ship: dict[str, float] = {}
        self._range_notice_baseline_by_ship: dict[str, float] = {}
        self._current_loadout_modules: list[dict[str, Any]] = []
        self._last_plan_filter: str = ""
        self._watch_active: bool = True
        self._catalog: dict[str, Any] = {}
        self._engineer_registry: dict[str, Any] = {}
        self._module_names: dict[str, str] = {}
        self._load_catalog()
        self._initialize_database()
        self._last_import: dict[str, Any] | None = self._load_last_import()
        self._import_state: str = IMPORT_STATE_IMPORTED if self._last_import else IMPORT_STATE_IDLE
        self.settings_config: PluginSettings | None = self._build_settings_config()
        _JournalWatcher(self).start()

    def _load_catalog(self) -> None:
        """Load the versioned catalog, engineer registry and module names."""
        try:
            self._catalog = json.loads(
                Path(get_asset_path("module_catalog.json")).read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as error:
            log("warn", f"Ship Upgrade Manager catalog load failed: {error}")
            self._catalog = {"engineering_blueprints": []}
        try:
            self._engineer_registry = json.loads(
                Path(get_asset_path("ship_engineers.json")).read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as error:
            log("warn", f"Ship Upgrade Manager engineer registry load failed: {error}")
            self._engineer_registry = {}
        try:
            self._module_names = json.loads(
                Path(get_asset_path("module_names.json")).read_text(encoding="utf-8")
            ).get("names", {})
        except (OSError, json.JSONDecodeError) as error:
            log("warn", f"Ship Upgrade Manager module names load failed: {error}")
            self._module_names = {}

    # ------------------------------------------------------------------
    # Settings UI construction (import tunnel state machine)
    # ------------------------------------------------------------------

    def _build_settings_config(self) -> PluginSettings:
        return {
            "key": self.plugin_manifest.guid,
            "label": "plugin.sum.label",
            "icon": "rocket_launch",
            "grids": self._build_grids(),
        }

    def _build_grids(self, error: tuple[str, dict[str, str | int | float]] | None = None) -> list[SettingsGrid]:
        grids: list[SettingsGrid] = []
        if self._import_state != IMPORT_STATE_IDLE:
            grids.append({
                "key": "import",
                "label": "plugin.sum.grid.import",
                "fields": self._import_fields(error),
            })
        plans_grid: SettingsGrid = {
            "key": "plans",
            "label": "plugin.sum.grid.plans",
            "fields": self._plans_fields(),
            "header_action": {
                "key": "import_plan",
                "icon": "add",
                "label": "plugin.sum.btn.import",
            },
        }
        grids.append(plans_grid)
        grids.append({
            "key": "session",
            "label": "plugin.sum.grid.session",
            "fields": self._session_fields(),
        })
        # The import entry point lives in the plans grid header; it is
        # redundant while the tunnel itself is open.
        if self._import_state != IMPORT_STATE_IDLE:
            plans_grid.pop("header_action", None)
        return grids

    def _settings_config(self) -> PluginSettings:
        config = self.settings_config
        assert config is not None, "settings_config is built in __init__"
        return config

    def _sync_grids(self, error: tuple[str, dict[str, str | int | float]] | None = None) -> None:
        config = self._settings_config()
        config["grids"] = self._build_grids(error)
        for grid in config["grids"]:
            for field in grid["fields"]:
                if field["type"] in {"paragraph", "error"}:
                    self.settings[field["key"]] = field.get("content", "")

    def _button(self, key: str, label: str, icon: str | None = None) -> ButtonSetting:
        field: ButtonSetting = {
            "key": key,
            "label": label,
            "type": "button",
            "readonly": False,
            "placeholder": None,
        }
        if icon:
            field["icon"] = icon
        return field

    def _paragraph(
        self,
        key: str,
        content: str,
        label: str | None = None,
        params: dict[str, str | int | float] | None = None,
    ) -> ParagraphSetting:
        field: ParagraphSetting = {
            "key": key,
            "label": label,
            "type": "paragraph",
            "readonly": True,
            "placeholder": None,
            "content": content,
        }
        if params:
            field["params"] = params
        return field

    def _import_fields(
        self,
        error: tuple[str, dict[str, str | int | float]] | None = None,
    ) -> list[SettingField]:
        state = self._import_state
        if state == IMPORT_STATE_IDLE:
            # The import entry point lives in the plans grid header action;
            # the tunnel grid itself only exists while a flow is open.
            return []
        if state == IMPORT_STATE_DATA:
            textarea: TextAreaSetting = {
                "key": "plan_input",
                "label": "plugin.sum.planDataLabel",
                "type": "textarea",
                "readonly": False,
                "placeholder": "plugin.sum.planDataPlaceholder",
                "default_value": "",
                "rows": 8,
                "cols": 60,
            }
            fields: list[SettingField] = [
                textarea,
                self._button("save_plan", "plugin.sum.btn.import"),
                self._button("cancel_import", "common.cancel"),
            ]
        elif state == IMPORT_STATE_IMPORTED:
            record = self._last_import or {}
            fields = [
                self._paragraph(
                    "import_done",
                    "plugin.sum.msg.doneBanner",
                    params={
                        "plan": record.get("plan_name", ""),
                        "version": record.get("version", 1),
                        "modules": record.get("modules", 0),
                        "sessions": record.get("sessions", 0),
                    },
                ),
                self._button("new_import", "plugin.sum.btn.newImport"),
            ]
        else:
            fields = []
        if error is not None:
            fields.append(self._paragraph("import_error", error[0], params=error[1]))
        return fields

    def _plans_fields(self) -> list[SettingField]:
        plan_filter: FilterSetting = {
            "key": "plan_filter",
            "label": "plugin.sum.search",
            "type": "filter",
            "readonly": False,
            "placeholder": "plugin.sum.searchPlaceholder",
            "default_value": "",
            "max_length": 100,
            "min_length": 0,
            "hidden": False,
        }
        plans_list: ListSetting = {
            "key": "available_plans",
            "label": None,
            "type": "list",
            "readonly": True,
            "placeholder": "plugin.sum.noPlans",
            "items": self._make_plan_rows(),
            "unit": "plan",
            "row_actions": [
                {
                    "action": "rename_plan",
                    "icon": "edit",
                    "label": "plugin.sum.btn.rename",
                    "inline_edit": True,
                },
                {
                    "action": "start_plan_session",
                    "icon": "play_arrow",
                    "label": "plugin.sum.btn.startSession",
                },
                {
                    "action": "delete_plan",
                    "icon": "delete",
                    "label": "plugin.sum.btn.delete",
                    "danger": True,
                }
            ],
        }
        delete_status = self._paragraph("plans_status", str(self.settings.get("plans_status", "") or ""))
        return [plan_filter, plans_list, delete_status]

    def _session_fields(self) -> list[SettingField]:
        session_modules: ListSetting = {
            "key": "session_modules",
            "label": None,
            "type": "list",
            "readonly": True,
            "placeholder": "plugin.sum.noSession",
            "items": self._session_module_rows(),
            "row_actions": [],
            "unit": "module",
        }
        return [
            self._paragraph(
                "session_summary",
                str(self.settings.get("session_summary", "") or "plugin.sum.noSession"),
                label="plugin.sum.session",
            ),
            session_modules,
        ]

    def _split_module_id(self, item: str) -> tuple[str, str | None, str | None]:
        """Split an FD module id into (family, size, class).

        Strips the int_/hpt_/hpt_cr_ prefix, then the _sizeN_classN tail when
        present. Trailing variant words stay attached to the family."""
        cleaned = self._normalize_module_id(item)
        prefix = ""
        for candidate in ("hpt_cr_", "hpt_", "int_"):
            if cleaned.startswith(candidate):
                prefix = candidate
                break
        head = cleaned[len(prefix):] if prefix else cleaned
        size = module_class = None
        variant = ""
        # Strip weapon mount suffix (hpt_: _fixed/gimbal/turret[_small/…/huge])
        if prefix.startswith("hpt"):
            head = re.sub(
                r"_(?:fixed|gimbal|turret)(?:_(?:small|medium|large|huge|tiny))?$", "", head
            )
        parts = re.split(r"_size(\d+)_class(\d+)", head, maxsplit=1)
        family = parts[0]
        if len(parts) > 1:
            size, module_class = parts[1], parts[2]
            # Spec form `..._sizeN_classM_<variant>`: the trailing segment
            # names the variant ("..._agile" -> Mk II Agile Boost).
            leftover = parts[3] if len(parts) > 3 else ""
            if leftover:
                variant = leftover.lstrip("_")
        else:
            class_match = re.search(r"_class(\d+)$", head)
            if class_match:
                family = head[: class_match.start()]
                module_class = class_match.group(1)
        if variant:
            family = f"{family}_{variant}"
        return family, size, module_class

    def _module_category(self, item: str, slot: str = "") -> str:
        """Outfitting-style category of a module, from its slot or FD id.

        The slot is checked first: core internal slots have well-known names
        regardless of the item's family (ship-specific variants like
        `MkIIAgileBoost_Engine` or armour ids don't match the generic
        family list)."""
        slot_norm = (slot or "").strip().lower()
        if slot_norm in CORE_SLOTS:
            return "plugin.sum.category.coreInternal"
        cleaned = self._normalize_module_id(item)
        if cleaned.startswith("hpt_cr_"):
            return "plugin.sum.category.utilityMounts"
        family, _, _ = self._split_module_id(item)
        if any(name in family for name in UTILITY_MODULE_FAMILIES):
            return "plugin.sum.category.utilityMounts"
        if cleaned.startswith("hpt_"):
            return "plugin.sum.category.hardpoints"
        if family in CORE_MODULE_FAMILIES:
            return "plugin.sum.category.coreInternal"
        return "plugin.sum.category.optionalInternal"

    def _session_module_rows(self) -> list[ListRow]:
        """Per-step detail of the current ship's plan in progress: target
        module vs the module currently carried on the ship, grouped by
        outfitting category."""
        session = self.get_session()
        if session is None:
            return []
        steps = session["steps"]
        loadout = self._loadout_for_ship(session["ship_instance_id"])
        matches = self._match_steps(steps, loadout)
        rows: list[ListRow] = []
        for match in matches:
            step = match["step"]
            item = str(step.get("item") or step.get("id", ""))
            title, title_key, title_grade = self._module_display(item)
            required = step.get("engineering") or {}
            required_level = required.get("Level") or required.get("level")
            required_blueprint = str(
                required.get("BlueprintName") or required.get("blueprint") or ""
            )
            required_special = str(
                required.get("ExperimentalEffect")
                or required.get("experimental_effect")
                or required.get("experimentalEffect")
                or ""
            )
            target_params: dict[str, str | int | float] = {
                "blueprint": _engineering_catalog_display_name(
                    required_blueprint, "blueprints.json", "fdname"
                ),
            }
            if required_special:
                target_params["experimentalEffect"] = _engineering_catalog_display_name(
                    required_special, "specials.json", "edname"
                )
            if required_level is not None:
                target_params["target"] = int(float(required_level))
            if not match["installed"]:
                meta = (
                    "plugin.sum.sessionStep.targetAbsent"
                    if required_level is not None
                    else "plugin.sum.sessionStep.absent"
                )
                params: dict[str, str | int | float] | None = (
                    target_params if required_level is not None else None
                )
            elif required_level is None:
                meta = "plugin.sum.sessionStep.installed"
                params = None
            else:
                current_level = match["eng_current"]
                has_engineering = bool(match["eng_blueprint_id"]) and bool(current_level)
                blueprint_ok = match["satisfied"] or (
                    has_engineering
                    and self._blueprint_key(match["eng_blueprint_id"])
                    == self._blueprint_key(required_blueprint)
                )
                target_params["current"] = current_level or 0
                if match["satisfied"]:
                    meta = "plugin.sum.sessionStep.targetDone"
                    params = target_params
                elif not has_engineering:
                    meta = "plugin.sum.sessionStep.targetNotEngineered"
                    params = target_params
                elif not blueprint_ok:
                    meta = "plugin.sum.sessionStep.targetOther"
                    params = target_params
                else:
                    meta = "plugin.sum.sessionStep.targetProgress"
                    params = target_params
            row: ListRow = {
                "key": str(step["id"]),
                "title": title,
                "title_key": title_key,
                "grade": title_grade or "",
                "group": self._module_category(item, str(step.get("slot") or "")),
                "meta": meta,
                "params": params or {},
                "pending": not match["satisfied"],
                "status": (
                    "absent"
                    if not match["installed"]
                    else "installed-pending"
                    if not match["satisfied"]
                    else "complete"
                ),
            }
            rows.append(row)
        category_index = {key: index for index, key in enumerate(CATEGORY_ORDER)}
        rows.sort(key=lambda row: (category_index.get(str(row.get("group")), 99), row["title"]))
        return rows

    def _grid(self, key: str) -> SettingsGrid:
        return next(grid for grid in self._settings_config()["grids"] if grid["key"] == key)

    def _field(self, grid_key: str, field_key: str) -> SettingField:
        return next(
            field for field in self._grid(grid_key)["fields"] if field["key"] == field_key
        )

    def _list_field(self, grid_key: str, field_key: str) -> ListSetting:
        field = self._field(grid_key, field_key)
        assert field["type"] == "list", f"{grid_key}.{field_key} is not a list field"
        return cast(ListSetting, field)

    def _enter_import_state(
        self,
        state: str,
        error: tuple[str, dict[str, str | int | float]] | None = None,
    ) -> None:
        self._import_state = state
        self._sync_grids(error)

    def _ship_display_name(self, internal: str) -> str:
        if internal in SHIP_NAMES:
            return SHIP_NAMES[internal]
        return internal.replace("_", " ").strip().title()

    def _make_plan_rows(self) -> list[ListRow]:
        filter_value = str(self.settings.get("plan_filter", "")).strip().lower()
        self._last_plan_filter = filter_value
        plans = [
            plan
            for plan in self.list_plans()
            if not filter_value
            or filter_value in plan["plan_name"].lower()
            or filter_value in plan["ship_model"].lower()
            or filter_value in self._ship_display_name(plan["ship_model"]).lower()
        ]
        return [
            {
                "key": plan["id"],
                "title": plan["plan_name"],
                "group": self._ship_display_name(plan["ship_model"]),
                "meta": f"v{plan['plan_version']} · {self._plan_module_count(plan['id'])} modules",
                "progress": self._plan_progress(plan["id"], plan["ship_model"]),
            }
            for plan in plans
        ]

    def _pretty_module_name(self, item: str) -> str:
        """Humanized fallback name for an internal module id (no size/grade)."""
        name, _key, _grade = self._module_display(item)
        return name

    def _module_display(self, item: str) -> tuple[str, str, str | None]:
        """Split an FD module id into display parts.

        Returns (display name, i18n key, size/class grade). The display name
        resolves against the module_names.json dictionary first (spec EN
        names), then the engineering catalogue, then a humanized fallback."""
        family, size, module_class = self._split_module_id(item)
        family_key = re.sub(r"[^a-z0-9]", "", family)
        key = f"module.{_spec_module_key(family_key)}"
        display = (
            "Reactive Surface Composite"
            if canonical_item(self._normalize_module_id(item)) == "reactive_surface_composite"
            else self._module_names.get(family_key)
        )
        if display is None:
            display = next(
                (name for name_key, name in MODULE_FAMILIES if family_key.startswith(name_key)),
                None,
            )
        if display is None:
            display = family.replace("_", " ").strip().title()
        grade = None
        if module_class:
            letter = CLASS_LETTERS.get(int(module_class))
            grade = f"{size}{letter}" if size and size != "0" else (letter or "?")
        return display, key, grade

    def _loadout_for_ship(self, ship_instance_id: str) -> list[dict[str, Any]]:
        """Current modules of a ship: live runtime snapshot, else journal read."""
        if self._current_loadout_modules and str(self._current_ship_id) == str(
            ship_instance_id
        ):
            return self._current_loadout_modules
        return self._loadout_baseline_for_ship(ship_instance_id)

    def _match_steps(
        self, steps: list[dict[str, Any]], modules: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """One-to-one match of plan steps against the ship's loadout.

        Each result carries the step, whether a matching module is installed,
        the currently reached engineering level and blueprint ID, and whether
        the step requirement is satisfied (strict blueprint rule)."""
        available: list[dict[str, Any]] = []
        for module in modules:
            item = self._normalize_module_id(str(module.get("Item") or ""))
            if not item or item == "null":
                continue
            engineering = module.get("Engineering") or {}
            available.append(
                {
                    "item": item,
                    "slot": str(module.get("Slot") or "").lower(),
                    "blueprint": str(
                        engineering.get("BlueprintName")
                        or engineering.get("blueprint")
                        or ""
                    ),
                    "level": engineering.get("Level") or engineering.get("level"),
                    "quality": engineering.get("Quality"),
                    "experimental_effect": self._experimental_effect(engineering),
                }
            )
        used = [False] * len(available)
        results: list[dict[str, Any]] = []
        for step in steps:
            item = self._normalize_module_id(
                str(step.get("item") or step.get("id") or "")
            )
            step_slot = str(step.get("slot") or "").lower()
            candidate_index = next(
                (
                    index
                    for index, candidate in enumerate(available)
                    if not used[index]
                    and canonical_item(candidate["item"]) == canonical_item(item)
                    and (
                        not step_slot
                        or not candidate["slot"]
                        or candidate["slot"] == step_slot
                    )
                ),
                None,
            )
            if candidate_index is None:
                candidate_index = next(
                    (
                        index
                        for index, candidate in enumerate(available)
                        if not used[index]
                        and canonical_item(candidate["item"]) == canonical_item(item)
                    ),
                    None,
                )
            if candidate_index is None:
                    results.append(
                        {
                            "step": step,
                            "installed": False,
                            "slot": "",
                            "eng_current": None,
                            "eng_blueprint_id": "",
                            "eng_quality": None,
                            "satisfied": False,
                        }
                    )
                    continue
            used[candidate_index] = True
            candidate = available[candidate_index]
            required = step.get("engineering") or {}
            required_level = required.get("Level") or required.get("level")
            required_blueprint = str(
                required.get("BlueprintName") or required.get("blueprint") or ""
            )
            level = candidate["level"]
            current_level = int(level) if isinstance(level, (int, float)) else None
            if required_level is None:
                satisfied = (
                    not required_blueprint
                    or self._blueprint_key(candidate["blueprint"])
                    == self._blueprint_key(required_blueprint)
                )
                eng_current = None
                eng_completed = None
            else:
                blueprint_ok = required_blueprint and self._blueprint_key(
                    candidate["blueprint"]
                ) == self._blueprint_key(required_blueprint)
                satisfied = bool(
                    blueprint_ok
                    and grade_reached(current_level, candidate["quality"], required_level)
                )
                eng_current = current_level if blueprint_ok else 0
                # Only fully completed grades (Quality 1.0) give their effect.
                eng_completed = (
                    0 if not blueprint_ok or current_level is None
                    else current_level
                    if grade_reached(current_level, candidate["quality"], current_level)
                    else current_level - 1
                )
            required_effect = self._experimental_effect(required)
            if required_effect and candidate["experimental_effect"].casefold() != required_effect.casefold():
                satisfied = False
            results.append(
                {
                    "step": step,
                    "installed": True,
                    "slot": candidate["slot"],
                    "eng_current": eng_current,
                    "eng_completed": eng_completed,
                    "eng_blueprint_id": candidate["blueprint"],
                    "eng_level": current_level,
                    "eng_quality": candidate["quality"],
                    "eng_experimental_effect": candidate["experimental_effect"],
                    "satisfied": satisfied,
                }
            )
        return results

    def _progress_metrics(
        self, steps: list[dict[str, Any]], modules: list[dict[str, Any]]
    ) -> dict[str, int]:
        """Two-criteria progress of a plan against the ship's current loadout.

        Modules: installed steps matched one-to-one against the loadout (slot
        preferred when known). Engineering: for steps that require it, the sum
        of target levels vs the sum of completed levels (Quality 1.0) — a
        level only counts when the blueprint matches the target (strict)."""
        matches = self._match_steps(steps, modules)
        modules_done = sum(1 for match in matches if match["installed"])
        eng_target = 0
        eng_current = 0
        for match in matches:
            required = match["step"].get("engineering") or {}
            required_level = required.get("Level") or required.get("level")
            if required_level is None:
                continue
            # The target sums every engineering step of the plan, whether or
            # not the module is installed yet.
            eng_target += int(float(required_level))
            if match.get("eng_completed") is not None:
                eng_current += match["eng_completed"]
        total = len(steps)
        return {
            "modules_done": modules_done,
            "modules_total": total,
            "modules_pct": round((modules_done / total) * 100) if total else 0,
            "eng_current": eng_current,
            "eng_target": eng_target,
            "eng_pct": round((eng_current / eng_target) * 100) if eng_target else 0,
        }

    def _module_importance(self, step: dict[str, Any]) -> tuple[int, int]:
        """Sort key of a plan step by install importance (lower first)."""
        slot = re.sub(r"[^a-z0-9]", "", str(step.get("slot") or "").casefold())
        item = self._normalize_module_id(str(step.get("item") or ""))
        family, size, _module_class = self._split_module_id(item) if item else ("", None, None)
        family_key = re.sub(r"[^a-z0-9]", "", family)
        core_slot = slot if slot in CORE_SLOT_ORDER else None
        if core_slot is None and not slot:
            core_slot = "armour" if "armour" in item else next(
                (
                    core for prefix, core in CORE_FAMILY_SLOTS.items()
                    if family_key.startswith(prefix)
                ),
                None,
            )
        if core_slot is not None:
            return (0, CORE_SLOT_ORDER.index(core_slot))
        if family_key.startswith("shieldgenerator"):
            return (1, 0)
        mount = re.search(r"(huge|large|medium|small)hardpoint", slot) or (
            None if slot else re.search(r"_(huge|large|medium|small)$", item)
        )
        if mount:
            return (2, -HARDPOINT_MOUNT_SIZES[mount.group(1)])
        if "tinyhardpoint" in slot or item.startswith("hpt_"):
            return (4, 0)
        slot_size = re.search(r"size(\d+)", slot)
        module_size = int(slot_size.group(1)) if slot_size else int(size or 0)
        return (3, -module_size)

    def _missing_module_steps(
        self, steps: list[dict[str, Any]], modules: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Plan steps whose target module is not installed at all."""
        return [match["step"] for match in self._match_steps(steps, modules) if not match["installed"]]

    def _missing_modules_summary(
        self,
        steps: list[dict[str, Any]],
        modules: list[dict[str, Any]],
        list_all: bool = False,
    ) -> str:
        """Spoken summary of the target modules not installed on the ship.

        A few missing modules are named directly; more are counted and the
        most important ones are proposed first (all of them with list_all)."""
        missing = self._missing_module_steps(steps, modules)
        total = len(steps)
        if not missing:
            return f"All {total} target modules are installed."
        ranked = sorted(missing, key=self._module_importance)
        if list_all or len(missing) <= MISSING_MODULES_NAMED:
            names = "; ".join(self._step_short_label(step) for step in ranked)
            return (
                f"{len(missing)} of {total} target modules are not installed yet: {names}."
            )
        top = "; ".join(
            self._step_short_label(step) for step in ranked[:MISSING_MODULES_NAMED]
        )
        return (
            f"{len(missing)} of {total} target modules are not installed yet. "
            f"The most important to install first: {top}. Give the player the count "
            "and offer to install these first."
        )

    def _plan_progress(
        self, plan_id: str, ship_model: str, ship_instance_id: str | None = None
    ) -> list[ListRowProgress]:
        ship_filter = " AND ship_instance_id = ?" if ship_instance_id is not None else ""
        with self.get_db() as db:
            plan_row = db.execute(
                "SELECT source_json FROM plans WHERE id = ?", (plan_id,)
            ).fetchone()
            sessions = db.execute(
                f"""
                SELECT ship_custom_name, ship_instance_id, completed_steps, paused, last_activity
                FROM active_session WHERE plan_id = ?{ship_filter} ORDER BY last_activity DESC
                """,
                (plan_id,) if ship_instance_id is None else (plan_id, ship_instance_id),
            ).fetchall()
        if plan_row is None or not sessions:
            return []
        steps = self._plan_steps(plan_row["source_json"])
        display_model = self._ship_display_name(ship_model)
        progress: list[ListRowProgress] = []
        for session in sessions:
            completed = json.loads(session["completed_steps"] or "[]")
            completed_set = set(completed)
            next_step = next((step for step in steps if step["id"] not in completed_set), None)
            custom_name = session["ship_custom_name"] or ""
            entry: ListRowProgress = {
                # No custom name yet: show the public ship model instead of the
                # raw journal ship id.
                "ship": custom_name or display_model,
                "ship_model": display_model if custom_name else "",
                "paused": bool(session["paused"]),
            }
            metrics = self._progress_metrics(steps, self._loadout_for_ship(session["ship_instance_id"]))
            entry["modules_done"] = metrics["modules_done"]
            entry["modules_total"] = metrics["modules_total"]
            entry["modules_pct"] = metrics["modules_pct"]
            entry["eng_current"] = metrics["eng_current"]
            entry["eng_target"] = metrics["eng_target"]
            entry["eng_pct"] = metrics["eng_pct"]
            if next_step is not None:
                next_item = next_step.get("item")
                if next_item:
                    next_name, next_key, next_class = self._module_display(str(next_item))
                    entry["next_label"] = next_name
                    entry["next_key"] = next_key
                    entry["next_class"] = next_class or ""
                else:
                    entry["next_label"] = str(
                        next_step.get("label") or next_step.get("id", "")
                    )
                engineering = next_step.get("engineering") or {}
                grade = engineering.get("Level") or engineering.get("level")
                if grade is not None:
                    entry["next_grade"] = grade
                blueprint = str(
                    engineering.get("BlueprintName")
                    or engineering.get("blueprint")
                    or ""
                ).strip()
                if blueprint and blueprint.lower() != "none":
                    entry["next_engineering"] = _engineering_catalog_display_name(
                        blueprint, "blueprints.json", "fdname"
                    )
                experimental_effect = str(
                    engineering.get("ExperimentalEffect")
                    or engineering.get("experimental_effect")
                    or engineering.get("experimentalEffect")
                    or ""
                )
                if experimental_effect:
                    entry["next_experimental_effect"] = (
                        _engineering_catalog_display_name(
                            experimental_effect, "specials.json", "edname"
                        )
                    )
            progress.append(entry)
        return progress

    def _set_plan_rows(self, rows: list[ListRow]) -> None:
        self._list_field("plans", "available_plans")["items"] = rows

    def _set_message_field(self, grid_key: str, field_key: str, content: str, params: dict[str, str | int | float] | None = None) -> None:
        field = cast(ParagraphSetting, self._field(grid_key, field_key))
        field["content"] = content
        if params:
            field["params"] = params
        else:
            field.pop("params", None)
        self.settings[field_key] = content

    # ------------------------------------------------------------------
    # Last-import persistence (diff kept visible across restarts)
    # ------------------------------------------------------------------

    def _load_last_import(self) -> dict[str, Any] | None:
        with self.get_db() as db:
            row = db.execute(
                "SELECT value FROM plugin_meta WHERE key = ?", (LAST_IMPORT_META_KEY,)
            ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["value"])
        except (TypeError, json.JSONDecodeError):
            return None

    def _save_last_import(self, record: dict[str, Any]) -> None:
        with self.get_db() as db:
            db.execute(
                "INSERT OR REPLACE INTO plugin_meta (key, value) VALUES (?, ?)",
                (LAST_IMPORT_META_KEY, json.dumps(record)),
            )

    def _clear_last_import(self) -> None:
        self._last_import = None
        with self.get_db() as db:
            db.execute("DELETE FROM plugin_meta WHERE key = ?", (LAST_IMPORT_META_KEY,))

    def _reload_state_from_database(self) -> None:
        """Re-derive the import state once the runtime data path is known."""
        last_import = self._load_last_import()
        if last_import == self._last_import:
            return
        self._last_import = last_import
        if last_import is None:
            if self._import_state == IMPORT_STATE_IMPORTED:
                self._import_state = IMPORT_STATE_IDLE
        elif self._import_state in {IMPORT_STATE_IDLE, IMPORT_STATE_IMPORTED}:
            self._import_state = IMPORT_STATE_IMPORTED
        else:
            return
        self._sync_grids()

    @override
    def on_chat_start(self, helper: PluginHelper) -> None:
        self.helper = helper
        self._watch_active = False
        self._initialize_database()
        self._reload_state_from_database()
        helper.register_projection(ShipUpgradeProjection())
        helper.register_event(
            "ShipUpgradeNotice",
            lambda _event: True,
            lambda event: (
                "Relay this ship-upgrade notice to the commander without "
                "calling route-planning or station-search tools. Recalculate "
                "the route only after an explicit user request: "
                + str(event.plugin_event_content)
            ),
        )
        helper.register_action(
            name="ship_upgrade_start_session",
            description="Start a ship upgrade plan for the current ship",
            parameters=StartSessionParams,
            method=self._start_session_action,
            action_type="ship",
        )
        helper.register_action(
            name="ship_upgrade_complete_step",
            description="Mark a ship upgrade module as completed",
            parameters=CompleteStepParams,
            method=self._complete_step_action,
            action_type="ship",
        )
        helper.register_action(
            name="ship_upgrade_pause_session",
            description="Pause the active ship upgrade session",
            parameters=SessionActionParams,
            method=lambda args, context: self._session_pause_action(args.session_id, context),
            action_type="ship",
        )
        helper.register_action(
            name="ship_upgrade_resume_session",
            description="Resume the active ship upgrade session",
            parameters=SessionActionParams,
            method=lambda args, context: self._session_resume_action(args.session_id, context),
            action_type="ship",
        )
        helper.register_action(
            name="ship_upgrade_stop_session",
            description="Stop the active ship upgrade session",
            parameters=SessionActionParams,
            method=lambda args, context: self._session_stop_action(args.session_id, context),
            action_type="ship",
        )
        helper.register_action(
            name="ship_upgrade_next_step",
            description=(
                "Describe the next uncompleted module of the active ship upgrade "
                "session, including its target engineering blueprint, grade, and "
                "experimental effect when specified by the plan. Use this action "
                "for questions about the next planned upgrade or experimental "
                "effect. For 'what can I upgrade here/at this engineer', use "
                "ship_upgrade_workshop_upgrades instead. Never infer an experimental effect that is absent "
                "from the plan. The reply automatically warns when engineering "
                "materials are missing for this upgrade; relay that warning."
            ),
            parameters=SessionActionParams,
            method=lambda args, context: self._next_step_action(args.session_id, context),
            action_type="ship",
            cache_prefill={
                "j'ai un plan actif, quel effet expérimental dois-je appliquer": {},
                "quel effet expérimental dois-je appliquer": {},
                "quel effet expérimental appliquer au prochain module": {},
                "what experimental effect should i apply": {},
                "which experimental effect should i apply to the next module": {},
                "programme l'amélioration suivante": {},
                "programme la prochaine amélioration": {},
                "quelle est la prochaine amélioration": {},
                "schedule the next upgrade": {},
                "what is the next upgrade": {},
            },
        )
        helper.register_action(
            name="ship_upgrade_workshop_upgrades",
            description=(
                "For questions about improvements here, at this workshop or at "
                "this engineer, use this action first when a ship upgrade plan is "
                "active. Relay the planned blueprint, grade and experimental "
                "effect first, then clearly distinguish any other locally available "
                "blueprints as outside the plan, never as recommendations from it. "
                "Only consider installed modules covered by the plan. Do not "
                "substitute the next planned module if this engineer cannot modify "
                "it. Report grade/rank limits and unknown workshop or ship state."
            ),
            parameters=SessionActionParams,
            method=lambda args, context: self._workshop_upgrades_action(args.session_id, context),
            action_type="ship",
            cache_prefill={
                "quelles améliorations je peux faire ici": {},
                "quelles améliorations puis-je faire chez cet ingénieur": {},
                "quelle modification puis-je faire dans cet atelier": {},
                "quelle amélioration est-ce que je peux faire ici": {},
                "que puis-je améliorer ici selon mon plan": {},
                "what can i upgrade here": {},
                "what upgrades can i do at this engineer": {},
            },
        )
        helper.register_action(
            name="ship_upgrade_list_plans",
            description="List available ship upgrade plans",
            parameters=SessionActionParams,
            method=lambda _args, _context: self._list_plans_action(),
            action_type="ship",
        )
        helper.register_action(
            name="ship_upgrade_preview_changes",
            description="Preview changes in a ship upgrade plan",
            parameters=PlanChangeParams,
            method=self._preview_changes_action,
            action_type="ship",
        )
        helper.register_action(
            name="ship_upgrade_apply_changes",
            description="Apply a new ship upgrade plan and migrate its active session",
            parameters=PlanChangeParams,
            method=self._apply_changes_action,
            action_type="ship",
        )
        helper.register_action(
            name="ship_upgrade_build_advice",
            description=(
                "Prepare the complete ordered multi-stop route to finish the active "
                "or most recently imported target ship build, even if no tracking "
                "session has been activated yet. Compare every target module "
                "against the current loadout; find and group module purchases by "
                "Spansh outfitting stations; add material trader stops for planned "
                "exchanges; include unlocked engineer visits (one per engineer), all "
                "stops ordered as the shortest route with system names and distances "
                "from stop to stop, then list every blueprint grade and "
                "materials status. Call this when the player asks for the full "
                "route, complete route, all steps, or how to finish the build. "
                "For a direct question about the next blueprint, grade, or "
                "experimental effect, use ship_upgrade_next_step instead. "
                "For a full stop-by-stop route set detailed=true, including "
                "when the player asks 'détaille toute la route d'amélioration'. "
                "Otherwise relay the concise route summary, not every stop. "
                "If the player names one module, set the optional module argument "
                "and limit the route to that module. If no module is named, plan "
                "the complete target build. Never send a build-route request to "
                "web_search_agent: this action owns the route."
            ),
            parameters=BuildAdviceParams,
            method=self._build_advice_action,
            action_type="ship",
            input_template=lambda _args, _states: "Prepare complete ship-upgrade route",
            cache_prefill={
                "prepare a route for improving my ship": {},
                "prepare a route to improve my ship": {},
                "prepare a route to improve my spaceship": {},
                "prepare a route to upgrade my ship": {},
                "prepare a route to upgrade my entire ship": {},
                "make me a complete route to improve my entire ship": {},
                "plan a complete route to improve my whole ship": {},
                "prepare a complete route for improving my ship": {},
                "prepare a complete route for all modules": {},
                "prepare a complete route to improve the entire ship": {},
                "i want a full route for all modules": {},
                "make a complete route for my ship build": {},
                "build me a route to finish the target build": {},
                "plan the full route to complete my build": {},
                "what is the complete route for this build": {},
                "route complete for all modules": {},
                "prepare route for ship upgrades": {},
                "plan an upgrade route for my ship": {},
                "route complète pour améliorer mon vaisseau": {},
                "prépare-moi une route pour l'amélioration de mon vaisseau": {},
                "prépare une route pour améliorer mon vaisseau": {},
                "prépare moi une route pour améliorer mon vaisseau": {},
                "prépare une route d'amélioration de mon vaisseau": {},
                "programme-moi une route complète pour l'amélioration de mon vaisseau": {},
                "programme-moi une route pour l'amélioration de mon vaisseau": {},
                "programme une route complète pour améliorer mon vaisseau": {},
                "prépare une route complète pour améliorer l'intégralité de mon vaisseau": {},
                "prépare-moi une route complète pour améliorer l'intégralité de mon vaisseau": {},
                "prépare une route complète pour tous les modules": {},
                "je veux une route complète pour tous les modules": {},
                "fais-moi une route pour améliorer mon vaisseau": {},
                "fais-moi un itinéraire complet pour terminer le build": {},
                "donne-moi toutes les étapes pour améliorer tout le vaisseau": {},
                "toutes les étapes pour terminer mon build": {},
                "détaille toute la route d'amélioration": {"detailed": True},
                "donne le détail de la route d'amélioration": {"detailed": True},
                "detail the entire ship upgrade route": {"detailed": True},
            },
        )
        helper.register_action(
            name="ship_upgrade_plan_status",
            description=(
                "Answer whether a ship-upgrade plan is loaded, active or currently "
                "in progress. Report the plan name and ship; distinguish an imported "
                "target plan from an active tracking session. Use for questions like "
                "'is an upgrade plan loaded?', 'which plan is active/in progress?', "
                "or 'do I have a ship build plan?'."
            ),
            parameters=SessionActionParams,
            method=lambda _args, _context: self._plan_status_action(),
            action_type="ship",
            input_template=lambda _args, _states: "Check loaded ship-upgrade plan status",
            cache_prefill={
                "is an upgrade plan loaded": {},
                "do i have a ship upgrade plan loaded": {},
                "which upgrade plan is loaded": {},
                "which upgrade plan is active": {},
                "which plan is in progress": {},
                "what is my current ship build plan": {},
                "est-ce qu un plan d amelioration est charge": {},
                "est-ce qu il y a un plan d amelioration charge": {},
                "y a t il un plan d amelioration charge": {},
                "quel plan d amelioration est charge": {},
                "quel plan est actif": {},
                "quel plan est en cours": {},
                "mon plan d amelioration est il actif": {},
                "est-ce que j ai un plan d amelioration charge": {},
            },
        )
        helper.register_action(
            name="ship_upgrade_current_loadout",
            description=(
                "Answer questions about modules actually installed on the current ship. "
                "Use the loadout journal's technical slot and item identifiers, never "
                "compare module display names or localized strings. For an armor/hull "
                "question, query slot Armour and report its exact installed module id, "
                "recognized module name, engineering blueprint, and grade. Do not use "
                "the imported target plan to describe the current installed module. "
                "To know which target modules are missing, use "
                "ship_upgrade_missing_modules instead of comparing lists yourself."
            ),
            parameters=LoadoutQueryParams,
            method=self._current_loadout_action,
            action_type="ship",
            input_template=lambda _args, _states: "Check the current ship loadout",
            cache_prefill={
                "what armor module is installed": {"slot": "Armour"},
                "what armour module is installed": {"slot": "Armour"},
                "which armor do i have installed": {"slot": "Armour"},
                "which armour do i have installed": {"slot": "Armour"},
                "what is my installed armor": {"slot": "Armour"},
                "what is my installed armour": {"slot": "Armour"},
                "quel module de blindage est installé": {"slot": "Armour"},
                "quelle armure est installée": {"slot": "Armour"},
                "quelle armure ai-je déjà": {"slot": "Armour"},
                "quel est le module d'armure que j'ai déjà": {"slot": "Armour"},
                "quel module d'armure ai-je déjà": {"slot": "Armour"},
                "et quel est le module d'armure que j'ai déjà": {"slot": "Armour"},
                "quel module d'armure ai-je déjà": {"slot": "Armour"},
            },
        )
        helper.register_action(
            name="ship_upgrade_state",
            description=(
                "Report the current upgrade plan state: which plan, on which ship, "
                "progress, modules installed vs target, engineering remaining. "
                "For the specific blueprint or experimental effect to apply next, "
                "use ship_upgrade_next_step."
            ),
            parameters=StateQueryParams,
            method=self._state_query_action,
            action_type="ship",
        )
        helper.register_action(
            name="ship_upgrade_goal",
            description="Activate a plan as the current build goal and start a session",
            parameters=GoalParams,
            method=self._goal_declare_action,
            action_type="ship",
        )
        helper.register_action(
            name="ship_upgrade_missing_modules",
            description=(
                "Tell which target-build modules are not installed on the current "
                "ship, computed from the live loadout. Relay the answer as given: "
                "with 3 or fewer missing it names them; with more it gives the "
                "count and the most important ones, then offer to install those "
                "first. Never claim every module is installed unless this says so."
            ),
            parameters=MissingModulesParams,
            method=self._missing_modules_action,
            action_type="ship",
            input_template=lambda _args, _states: "Check missing target modules",
            cache_prefill={
                "quels modules me manquent": {},
                "quels sont les modules manquants": {},
                "quel module manque": {},
                "quel module me manque": {},
                "est-ce qu il me manque des modules": {},
                "est-ce qu'il me manque des modules": {},
                "il me manque des modules": {},
                "donne-moi la liste de tous les modules manquants": {"list_all": True},
                "which modules am i missing": {},
                "which module is missing": {},
                "am i missing any modules": {},
                "list all missing modules": {"list_all": True},
            },
        )
        helper.register_action(
            name="ship_upgrade_missing_materials",
            description=(
                "Compare the commander's engineering material stock with every "
                "material still needed by the remaining upgrades of the active or "
                "imported ship build (blueprint grades and experimental effects). "
                "Without count: reply how many material types must still be "
                "collected and the missing quantity of the top 3 only; never "
                "enumerate all missing materials. With count (the player asks for "
                "the N most missing materials): list those N materials with a plan "
                "to obtain each one (material trader exchanges from surplus stock, "
                "then known sources)."
            ),
            parameters=MissingMaterialsParams,
            method=self._missing_materials_action,
            action_type="ship",
            input_template=lambda _args, _states: "Check missing engineering materials",
            cache_prefill={
                "quels sont les matériaux manquants": {},
                "quels matériaux me manquent": {},
                "quels matériaux manque-t-il": {},
                "il me manque quels matériaux": {},
                "which materials am i missing": {},
                "what materials are missing": {},
                "which materials are missing for my build": {},
                "quels sont les 3 matériaux qui manquent le plus": {"count": 3},
                "quels sont les 5 matériaux qui manquent le plus": {"count": 5},
                "quels sont les 10 matériaux qui manquent le plus": {"count": 10},
                "what are the 5 most missing materials": {"count": 5},
                "what are the 10 most missing materials": {"count": 10},
            },
        )
        helper.register_action(
            name="ship_upgrade_material_shortfall",
            description=(
                "Report how many units of one named engineering material the "
                "remaining ship upgrades need, how many the commander has, how many "
                "are missing, which modules use it, and how to obtain the rest."
            ),
            parameters=MaterialQueryParams,
            method=self._material_shortfall_action,
            action_type="ship",
            input_template=lambda args, _states: f"Check material {args.get('material', '')}",
        )
        helper.register_action(
            name="ship_upgrade_step_materials",
            description=(
                "List the engineering materials (blueprint grades and experimental "
                "effect) required by the next ship upgrade, or by a named plan "
                "module, with need, stock and missing quantities. Use when the "
                "player asks to check whether they have the materials for the next "
                "upgrade or for the materials details of an upgrade."
            ),
            parameters=StepMaterialsParams,
            method=self._step_materials_action,
            action_type="ship",
            input_template=lambda _args, _states: "Check materials for the upgrade",
            cache_prefill={
                "ai-je les matériaux pour la prochaine amélioration": {},
                "vérifie les matériaux pour la prochaine amélioration": {},
                "est-ce que j'ai assez de matériaux pour l'amélioration suivante": {},
                "quels matériaux faut-il pour la prochaine amélioration": {},
                "do i have the materials for the next upgrade": {},
                "check materials for the next upgrade": {},
                "what materials does the next upgrade need": {},
            },
        )
        helper.register_sideeffect(self._on_event)
        helper.register_status_generator(self._status_generator)
        self._publish_status()

    @override
    def on_chat_stop(self, helper: PluginHelper) -> None:
        self.helper = None
        self._watch_active = True

    @override
    def on_settings_changed(self) -> None:
        plan_filter = self.settings.get("plan_filter", "")
        if plan_filter != getattr(self, "_last_plan_filter", ""):
            self._publish_status()

    @override
    def on_settings_button(self, key: str, value: str | None = None) -> None:
        if key == "import_plan":
            self._enter_import_state(IMPORT_STATE_DATA)
        elif key == "save_plan":
            self._import_from_settings()
        elif key == "cancel_import":
            self.settings["plan_input"] = ""
            self._enter_import_state(IMPORT_STATE_IDLE)
        elif key == "new_import":
            self.settings["plan_input"] = ""
            self._enter_import_state(IMPORT_STATE_DATA)
        elif key.startswith("rename_plan:"):
            self._rename_plan_from_settings(key.split(":", 1)[1], value or "")
        elif key.startswith("start_plan_session:"):
            self._start_plan_session_from_settings(key.split(":", 1)[1])
        elif key.startswith("delete_plan:"):
            self._delete_plan_by_id(key.split(":", 1)[1])
        else:
            log("warn", f"Unknown Ship Upgrade Manager settings button: {key}")

    def _start_session_action(
        self, args: StartSessionParams, _context: dict[str, Any]
    ) -> str:
        session = self.start_session(
            args.plan_name, args.ship_instance_id, args.ship_custom_name
        )
        return (
            f"Started {session['plan_name']} on "
            f"{session['ship_custom_name'] or session['ship_instance_id']}. "
            f"{session['total_steps']} steps available."
        )

    def _complete_step_action(
        self, args: CompleteStepParams, context: dict[str, Any]
    ) -> str:
        session = self.complete_step(
            args.step_id, args.notes, args.session_id, context
        )
        return (
            f"Completed {args.step_id}. "
            f"Progress: {len(session['completed_steps'])}/{session['total_steps']}."
        )

    def _session_pause_action(self, session_id: str | None = None, context: dict[str, Any] | None = None) -> str:
        self.set_session_paused(True, session_id, context)
        return "Ship upgrade session paused."

    def _session_resume_action(self, session_id: str | None = None, context: dict[str, Any] | None = None) -> str:
        self.set_session_paused(False, session_id, context)
        return "Ship upgrade session resumed."

    def _session_stop_action(self, session_id: str | None = None, context: dict[str, Any] | None = None) -> str:
        self.stop_session(session_id, context)
        return "Ship upgrade session stopped."

    def _step_voice_label(
        self, step: dict[str, Any], match: dict[str, Any] | None = None
    ) -> str:
        """Readable spoken label for a plan step: spec name with grade, plus
        the engineering target and experimental effect when present.

        With the step's loadout `match` (see _match_steps), a module already
        installed is announced as such and only the remaining work (blueprint
        grade, experimental effect) is spoken, so the player is not told to
        buy a module they already carry."""
        item = step.get("item")
        if item:
            name, _key, grade = self._module_display(str(item))
            label = f"{name} {grade}" if grade else name
        else:
            label = str(step.get("label") or step.get("id", ""))
        installed = bool(match and match.get("installed"))
        engineering = step.get("engineering") or {}
        blueprint_id = str(
            engineering.get("BlueprintName") or engineering.get("blueprint") or ""
        )
        level = engineering.get("Level") or engineering.get("level")
        experimental_effect_id = self._experimental_effect(engineering)
        remaining: list[str] = []
        effect_note = ""
        if blueprint_id and blueprint_id.lower() != "none":
            blueprint_name = _engineering_catalog_display_name(
                blueprint_id, "blueprints.json", "fdname"
            )
            current_blueprint = str((match or {}).get("eng_blueprint_id") or "")
            same_blueprint = bool(current_blueprint) and self._blueprint_key(
                current_blueprint
            ) == self._blueprint_key(blueprint_id)
            current_level = (match or {}).get("eng_level")
            if installed and same_blueprint and (
                level is None or grade_reached(
                    current_level, (match or {}).get("eng_quality"), level
                )
            ):
                pass
            elif level is None:
                remaining.append(f"engineer it with {blueprint_name}")
            elif installed and same_blueprint and current_level:
                if int(current_level) >= int(float(level)):
                    remaining.append(
                        f"complete the {blueprint_name} grade {int(float(level))} rolls"
                    )
                else:
                    remaining.append(
                        f"engineer it with {blueprint_name} from grade "
                        f"{int(current_level)} to grade {int(float(level))}"
                    )
            else:
                action = f"engineer it with {blueprint_name} grade {int(float(level))}"
                if installed and current_blueprint:
                    current_name = _engineering_catalog_display_name(
                        current_blueprint, "blueprints.json", "fdname"
                    )
                    action += f" (it currently carries {current_name})"
                remaining.append(action)
        if experimental_effect_id:
            current_effect = str((match or {}).get("eng_experimental_effect") or "")
            experimental_effect = _engineering_catalog_display_name(
                experimental_effect_id, "specials.json", "edname"
            )
            if installed and current_effect.casefold() == experimental_effect_id.casefold():
                effect_note = f" Experimental effect {experimental_effect} already installed."
            else:
                remaining.append(f"apply the experimental effect {experimental_effect}")
        elif blueprint_id:
            effect_note = " No experimental effect specified by the plan."
        if not installed:
            return label + "".join(f", then {action}" for action in remaining) + effect_note
        if not remaining:
            return f"{label}, already installed with the target engineering." + effect_note
        return f"{label}, already installed; remaining: " + ", then ".join(remaining) + effect_note

    def _step_loadout_match(
        self, step: dict[str, Any], steps: list[dict[str, Any]], ship_instance_id: str
    ) -> dict[str, Any] | None:
        """Loadout match of one plan step, assigned one-to-one against the
        whole plan so two identical target modules do not claim the same
        installed module."""
        if not ship_instance_id:
            return None
        matches = self._match_steps(steps, self._loadout_for_ship(ship_instance_id))
        return next(
            (match for match in matches if match["step"].get("id") == step.get("id")),
            None,
        )

    def _alternative_engineer(self, step: dict[str, Any], current: str) -> str | None:
        engineering = step.get("engineering") or {}
        blueprint = str(engineering.get("BlueprintName") or "")
        entry = catalog_entry(self._catalog, blueprint)
        grade = engineering.get("Level")
        if entry is None or grade is None:
            return None
        candidates = [
            engineer["name"]
            for engineer in _engineers_for_blueprint(
                entry, int(float(grade)), self._engineer_registry
            )
            if engineer["name"] != current
        ]
        progress = self._engineer_progress()
        candidates = [
            name for name in candidates
            if (progress or {}).get(name, {}).get("Progress") != "Barred"
        ]
        candidates.sort(key=lambda name: (
            0 if (progress or {}).get(name, {}).get("Progress") == "Unlocked"
            and (not isinstance((progress or {}).get(name, {}).get("Rank"), int)
                 or (progress or {})[name]["Rank"] >= int(float(grade))) else 1,
            name,
        ))
        if not candidates:
            return None
        name = candidates[0]
        state = (progress or {}).get(name, {})
        access = state.get("Progress")
        rank = state.get("Rank")
        if access and access != "Unlocked":
            return f"{name} (access: {access})"
        if isinstance(rank, int) and rank < int(float(grade)):
            return f"{name} (rank {rank}/{int(float(grade))} still needed)"
        if progress is None or not state:
            return f"{name} (unlock status unknown)"
        return name

    def _workshop_engineer(self, states: dict[str, Any] | None = None) -> str | None:
        if states is None:
            states = self._projected_states()
        location = states.get("Location") if states else None
        if isinstance(location, BaseModel):
            location = location.model_dump()
        if isinstance(location, dict):
            if location.get("Docked") is True:
                return self._engineer_at_workshop(
                    str(location.get("StarSystem") or ""),
                    location.get("Station"),
                )
            return None
        return self._current_workshop

    def _workshop_opportunities(
        self, session: dict[str, Any], engineer: str
    ) -> tuple[list[tuple[dict[str, Any], int]], list[str]]:
        remaining = [
            step for step in session["steps"]
            if step["id"] not in session["completed_steps"]
        ]
        progress = (self._engineer_progress() or {}).get(engineer, {})
        if progress.get("Progress") in {"Barred", "Known", "Invited", "Acquainted"}:
            return [], [f"{engineer} is not unlocked ({progress['Progress']})."]
        rank = progress.get("Rank")
        available: list[tuple[dict[str, Any], int]] = []
        limited: list[str] = []
        loadout = self._loadout_for_ship(str(session["ship_instance_id"]))
        for candidate in remaining:
            [delta] = analyze_build([candidate], loadout)
            if delta.status in {"missing", "module_different", "installed"}:
                continue
            entry = catalog_entry(self._catalog, delta.target_blueprint)
            grades = [grade for grade, _ in engineering_increments(delta)]
            if not grades and delta.target_experimental and delta.status == "experimental_behind":
                grades = [1]
            if not entry or not grades:
                continue
            supported = [
                grade for grade in grades
                if engineer in {
                    item["name"] for item in _engineers_for_blueprint(
                        entry, grade, self._engineer_registry
                    )
                }
            ]
            if not supported:
                continue
            feasible = [
                grade for grade in supported
                if not isinstance(rank, int) or rank >= grade
            ]
            if feasible:
                available.append((candidate, max(feasible)))
            if len(supported) < len(grades) or len(feasible) < len(supported):
                ceiling = max(supported, default=0)
                reason = f"maximum grade {ceiling}" if ceiling < max(grades) else ""
                if isinstance(rank, int) and rank < max(supported, default=0):
                    reason += f"; your unlocked rank is {rank}"
                alternative = self._alternative_engineer(candidate, engineer)
                if alternative:
                    reason += f"; {alternative} can handle the target grade"
                limited.append(f"{self._step_short_label(candidate)}: {reason.strip('; ')}")
        return available, limited

    def _workshop_alternatives(
        self, session: dict[str, Any], engineer: str
    ) -> list[str]:
        progress = (self._engineer_progress() or {}).get(engineer, {})
        if progress.get("Progress") in {"Barred", "Known", "Invited", "Acquainted"}:
            return []
        rank = progress.get("Rank")
        steps = [
            step for step in session["steps"]
            if step["id"] not in session["completed_steps"]
        ]
        matches = self._match_steps(
            steps, self._loadout_for_ship(str(session["ship_instance_id"]))
        )
        alternatives: list[str] = []
        for match in matches:
            if not match["installed"]:
                continue
            step = match["step"]
            planned = str((step.get("engineering") or {}).get("BlueprintName") or "")
            planned_entry = catalog_entry(self._catalog, planned)
            if not planned_entry:
                continue
            families = {
                re.sub(r"[^a-z0-9]", "", str(name).casefold())
                for name in planned_entry.get("module_names") or []
            }
            options: list[str] = []
            for entry in self._catalog.get("engineering_blueprints", []):
                blueprint = str(entry.get("fdname") or "")
                if not blueprint or blueprint.casefold() == planned.casefold():
                    continue
                if not families.intersection(
                    re.sub(r"[^a-z0-9]", "", str(name).casefold())
                    for name in entry.get("module_names") or []
                ):
                    continue
                grades = [
                    int(grade) for grade in entry.get("grades", {})
                    if grade.isdigit()
                    and (not isinstance(rank, int) or int(grade) <= rank)
                    and engineer in {
                        candidate["name"] for candidate in _engineers_for_blueprint(
                            entry, int(grade), self._engineer_registry
                        )
                    }
                ]
                if not grades:
                    continue
                if (
                    str(match.get("eng_blueprint_id") or "").casefold()
                    == blueprint.casefold()
                    and grade_reached(
                        match.get("eng_level"), match.get("eng_quality"), max(grades)
                    )
                ):
                    continue
                options.append(f"{entry['name']} (up to grade {max(grades)})")
            if options:
                alternatives.append(
                    f"{self._step_short_label(step)}: " + ", ".join(options)
                )
        return alternatives

    def _workshop_planned_offer(
        self, step: dict[str, Any], grade: int, loadout: list[dict[str, Any]]
    ) -> str:
        engineering = step.get("engineering") or {}
        blueprint = _engineering_catalog_display_name(
            str(engineering.get("BlueprintName") or ""), "blueprints.json", "fdname"
        )
        [delta] = analyze_build([step], loadout)
        if delta.status == "experimental_behind":
            offer = f"{self._step_short_label(step)}: apply the experimental effect"
        else:
            offer = f"{self._step_short_label(step)} with {blueprint} up to grade {grade}"
        if delta.target_experimental:
            effect = _engineering_catalog_display_name(
                delta.target_experimental, "specials.json", "edname"
            )
            if delta.status == "experimental_behind":
                offer += f" {effect}"
            elif (delta.current_experimental or "").casefold() == delta.target_experimental.casefold():
                offer += f" (experimental effect {effect} already installed)"
            else:
                offer += f" (experimental effect {effect} to apply)"
        return offer

    def _workshop_upgrades_action(
        self, session_id: str | None = None, context: dict[str, Any] | None = None
    ) -> str:
        engineer = self._workshop_engineer()
        if not engineer:
            return "I cannot identify a docked engineer workshop from the current location; I cannot confirm which planned upgrades are possible here."
        session = self.get_session(session_id, context)
        if session is None:
            return f"At {engineer}, there is no active ship upgrade session to compare."
        if session["paused"]:
            return f"At {engineer}, the ship upgrade session is paused."
        available, limited = self._workshop_opportunities(session, engineer)
        if not available:
            result = f"At {engineer}, no remaining planned upgrades can be performed on the installed modules now."
        else:
            loadout = self._loadout_for_ship(str(session["ship_instance_id"]))
            offers = [
                self._workshop_planned_offer(step, grade, loadout)
                for step, grade in available
            ]
            result = (
                f"At {engineer}, planned upgrades for {session['plan_name']}: "
                + "; ".join(offers) + "."
            )
        if limited:
            result += " Workshop limits: " + "; ".join(limited) + "."
        alternatives = self._workshop_alternatives(session, engineer)
        if alternatives:
            result += (
                " Other options at this workshop, NOT in the active plan "
                "(they would change the planned blueprint): "
                + "; ".join(alternatives) + "."
            )
        progress = (self._engineer_progress() or {}).get(engineer, {})
        if (available or alternatives) and (
            progress.get("Progress") != "Unlocked"
            or not isinstance(progress.get("Rank"), int)
        ):
            result += " Engineer unlock status or rank is unreported; grade availability is conditional."
        return result

    def _next_step_action(self, session_id: str | None = None, context: dict[str, Any] | None = None) -> str:
        session = self.get_session(session_id, context)
        if session is None:
            return "There is no active ship upgrade session."
        remaining = [
            step for step in session["steps"]
            if step["id"] not in session["completed_steps"]
        ]
        if not remaining:
            return f"Plan {session['plan_name']} is complete."
        step = remaining[0]
        workshop_note = ""
        engineer = self._workshop_engineer()
        if engineer:
            available, limited = self._workshop_opportunities(session, engineer)
            if available:
                step, local_grade = available[0]
                workshop_note = f" At {engineer}, you can work on this module up to grade {local_grade}."
                if step["id"] != remaining[0]["id"]:
                    alternative = self._alternative_engineer(remaining[0], engineer)
                    workshop_note += (
                        f" {self._step_short_label(remaining[0])} cannot be upgraded at this workshop."
                        + (f" Try {alternative} for its target grade." if alternative else "")
                    )
            else:
                return self._workshop_upgrades_action(session["id"], context) + (
                    " Ask for the full route to find a suitable engineer."
                )
            if limited:
                workshop_note += " Workshop limits: " + "; ".join(limited) + "."
        match = self._step_loadout_match(
            step, session["steps"], str(session["ship_instance_id"])
        )
        return (
            f"Next module, {len(session['completed_steps']) + 1} of "
            f"{session['total_steps']}: {self._step_voice_label(step, match)}."
            + workshop_note
            + self._next_step_materials_warning(step, session["ship_instance_id"])
        )

    def _list_plans_action(self) -> str:
        plans = self.list_plans()
        if not plans:
            return "No ship upgrade plans are imported."
        return "Available plans: " + "; ".join(
            f"{plan['plan_name']} for {plan['ship_model']} version {plan['plan_version']}"
            for plan in plans
        )

    def _step_matches_module_query(self, step: dict[str, Any], query: str) -> bool:
        normalized_query = re.sub(r"[^a-z0-9]", "", query.casefold())
        item = str(step.get("item") or "")
        if normalized_query in re.sub(r"[^a-z0-9]", "", item.casefold()):
            return True
        if item:
            display_name, module_key, _grade = self._module_display(item)
            if normalized_query in re.sub(r"[^a-z0-9]", "", display_name.casefold()):
                return True
            if normalized_query in re.sub(r"[^a-z0-9]", "", module_key.removeprefix("module.")):
                return True
        label = str(step.get("label") or step.get("id", ""))
        return normalized_query in re.sub(r"[^a-z0-9]", "", label.casefold())

    def _upgrade_target(self, session_id: str | None = None, context: dict[str, Any] | None = None) -> dict[str, Any] | str:
        """Active session target, else the latest imported plan for the
        current ship; a spoken explanation when neither is usable."""
        session = self.get_session(session_id, context)
        if session is not None:
            return {
                "plan_name": session["plan_name"],
                "steps": session["steps"],
                "completed": set(session["completed_steps"]),
                "ship_instance_id": session["ship_instance_id"],
                "ship_name": session.get("ship_custom_name") or None,
                "session": True,
            }
        # An imported target plan is sufficient to plan the build; do not
        # require the user to activate progress tracking.
        last_import = self._load_last_import()
        with self.get_db() as db:
            if last_import and last_import.get("plan_id"):
                plan_row = db.execute(
                    "SELECT id, plan_name, ship_model, source_json FROM plans WHERE id = ?",
                    (last_import["plan_id"],),
                ).fetchone()
            else:
                plan_row = db.execute(
                    "SELECT id, plan_name, ship_model, source_json FROM plans ORDER BY last_modified DESC LIMIT 1"
                ).fetchone()
        if plan_row is None:
            return "No target build is imported yet. Import a target build in the Plans settings first."
        plan = dict(plan_row)
        ship_instance_id = self._resolve_current_ship_id()
        if not ship_instance_id:
            return "I found the target build, but cannot identify the current ship from the Journal yet."
        return {
            "plan_name": plan["plan_name"],
            "steps": self._plan_steps(plan["source_json"]),
            "completed": set(),
            "ship_instance_id": ship_instance_id,
            "ship_name": self._current_ship_name or None,
            "session": False,
        }

    @staticmethod
    def _remaining_steps(target: dict[str, Any]) -> list[dict[str, Any]]:
        return [step for step in target["steps"] if step.get("id") not in target["completed"]]

    def _next_target_step(self, target: dict[str, Any]) -> dict[str, Any] | None:
        """Next upgrade: first uncompleted session step, else the first plan
        step the current loadout does not satisfy."""
        remaining = self._remaining_steps(target)
        if target["session"]:
            return remaining[0] if remaining else None
        loadout = self._loadout_for_ship(target["ship_instance_id"])
        return next(
            (step for step in remaining if not self._step_satisfied_by_loadout(step, loadout)),
            None,
        )

    def _material_inventory(self) -> dict[str, int] | None:
        """Ship engineering materials (Raw/Manufactured/Encoded) from the
        runtime Materials projection; None when unavailable."""
        states = self._projected_states()
        if states is None:
            return None
        return inventory_from_state(states.get("Materials"))

    def _projected_states(self) -> dict[str, Any] | None:
        """Current runtime projection states; None outside the runtime."""
        event_manager = getattr(self.helper, "_event_manager", None)
        if event_manager is None:
            return None
        _events, states = event_manager.get_current_state()
        return states

    def _step_material_needs(
        self, steps: list[dict[str, Any]], ship_instance_id: str
    ) -> list[StepNeed]:
        """Remaining engineering material needs of each step vs the loadout."""
        if not steps:
            return []
        deltas = analyze_build(steps, self._loadout_for_ship(ship_instance_id))
        reference = _material_reference()
        specials = _specials_catalog()
        return [
            step_need(str(step.get("id", "")), delta, self._catalog, specials, reference)
            for step, delta in zip(steps, deltas)
        ]

    def _step_short_label(self, step: dict[str, Any]) -> str:
        item = step.get("item")
        if item:
            name, _key, grade = self._module_display(str(item))
            return f"{name} {grade}" if grade else name
        return str(step.get("label") or step.get("id", ""))

    @staticmethod
    def _unknown_costs_note(needs: list[StepNeed]) -> str:
        unknown = sorted({entry for need in needs for entry in need.unknown})
        if not unknown:
            return ""
        return (
            " Material costs are unknown and not counted for: "
            + ", ".join(unknown)
            + "."
        )

    def _materials_overview(self, steps: list[dict[str, Any]], ship_instance_id: str) -> str:
        """One-sentence stock check for a set of steps (used by the route)."""
        inventory = self._material_inventory()
        if inventory is None:
            return ""
        needs = self._step_material_needs(steps, ship_instance_id)
        totals = aggregate_needs(needs)
        if not totals:
            return ""
        missing = shortfalls(totals, inventory, _material_reference())
        if not missing:
            return " Materials: everything required is in stock."
        return (
            f" Materials: {len(missing)} material types are missing "
            f"({sum(row.missing for row in missing)} units); ask for the most "
            "missing materials to get a collection plan."
        )

    def _next_step_materials_warning(self, step: dict[str, Any], ship_instance_id: str) -> str:
        """Automatic stock warning appended when the next upgrade is announced."""
        needs = self._step_material_needs([step], ship_instance_id)
        if not needs or not needs[0].engineering:
            return ""
        need = needs[0]
        unknown = self._unknown_costs_note(needs)
        inventory = self._material_inventory()
        if inventory is None:
            return (
                " Material stock could not be checked: the game has not reported "
                "the material inventory yet." + unknown
            )
        missing = shortfalls(aggregate_needs([need]), inventory, _material_reference())
        if not missing:
            return " Materials check: you have everything needed for this engineering." + unknown
        shown = ", ".join(f"{row.name} (missing {row.missing})" for row in missing[:3])
        more = f" and {len(missing) - 3} more" if len(missing) > 3 else ""
        return (
            f" Warning: materials are missing for this upgrade, {len(missing)} "
            f"types: {shown}{more}. Ask for this upgrade's materials for details."
            + unknown
        )

    def _missing_modules_action(
        self, args: MissingModulesParams, context: dict[str, Any] | None = None
    ) -> str:
        target = self._upgrade_target(args.session_id, context)
        if isinstance(target, str):
            return target
        loadout = self._loadout_for_ship(str(target["ship_instance_id"]))
        return f"{target['plan_name']}: " + self._missing_modules_summary(
            target["steps"], loadout, list_all=args.list_all
        )

    def _missing_materials_action(
        self, args: MissingMaterialsParams, context: dict[str, Any] | None = None
    ) -> str:
        target = self._upgrade_target(args.session_id, context)
        if isinstance(target, str):
            return target
        inventory = self._material_inventory()
        if inventory is None:
            return MATERIALS_UNAVAILABLE
        needs = self._step_material_needs(
            self._remaining_steps(target), target["ship_instance_id"]
        )
        unknown = self._unknown_costs_note(needs)
        totals = aggregate_needs(needs)
        plan_name = target["plan_name"]
        if not totals:
            return f"The remaining upgrades of {plan_name} need no engineering materials." + unknown
        reference = _material_reference()
        missing = shortfalls(totals, inventory, reference)
        if not missing:
            return (
                f"You already have all {len(totals)} material types needed for the "
                f"remaining upgrades of {plan_name}." + unknown
            )
        if args.count is None:
            top = "; ".join(f"{row.name}: {row.missing} missing" for row in missing[:3])
            return (
                f"{len(missing)} material types still need to be collected to complete "
                f"{plan_name} ({sum(row.missing for row in missing)} units in total). "
                f"The most missing: {top}." + unknown
            )
        count = max(1, min(int(args.count), 20))
        selected = missing[:count]
        plans = " ".join(
            f"{index}. {acquisition_plan(row, inventory, totals, reference)}"
            for index, row in enumerate(selected, 1)
        )
        return (
            f"The {len(selected)} most missing materials for {plan_name} "
            f"(out of {len(missing)} missing types): {plans} "
            "The nearest material trader can be located on request with a station search."
            + unknown
        )

    def _material_shortfall_action(
        self, args: MaterialQueryParams, context: dict[str, Any] | None = None
    ) -> str:
        target = self._upgrade_target(args.session_id, context)
        if isinstance(target, str):
            return target
        remaining = self._remaining_steps(target)
        needs = self._step_material_needs(remaining, target["ship_instance_id"])
        totals = aggregate_needs(needs)
        reference = _material_reference()
        material_id = reference.resolve(args.material, candidates=totals) or reference.resolve(args.material)
        if material_id is None:
            return (
                f"'{args.material}' does not match a single ship engineering "
                "material; ask the player for its full name."
            )
        name = reference.name(material_id)
        inventory = self._material_inventory()
        plan_name = target["plan_name"]
        if material_id not in totals:
            stock = f" You have {inventory.get(material_id, 0)}." if inventory is not None else ""
            return f"{name} is not needed by the remaining upgrades of {plan_name}.{stock}"
        total = totals[material_id]
        steps_by_id = {str(step.get("id", "")): step for step in remaining}
        used_by = ", ".join(
            self._step_short_label(steps_by_id[step_id])
            for step_id in total.steps if step_id in steps_by_id
        )
        if inventory is None:
            return (
                f"The remaining upgrades of {plan_name} need {total.needed} {name} "
                f"(used by {used_by}). {MATERIALS_UNAVAILABLE}"
            )
        row = next(row for row in material_status(totals, inventory, reference) if row.material_id == material_id)
        if row.missing == 0:
            return (
                f"You have enough {name} for {plan_name}: need {row.needed}, "
                f"have {row.have} (used by {used_by})."
            )
        return f"{acquisition_plan(row, inventory, totals, reference)} Used by {used_by}."

    def _step_materials_action(
        self, args: StepMaterialsParams, context: dict[str, Any] | None = None
    ) -> str:
        target = self._upgrade_target(args.session_id, context)
        if isinstance(target, str):
            return target
        if args.module and args.module.strip():
            step = next(
                (
                    candidate for candidate in self._remaining_steps(target)
                    if self._step_matches_module_query(candidate, args.module.strip())
                ),
                None,
            )
            if step is None:
                return f"No remaining upgrade of {target['plan_name']} matches '{args.module}'."
        else:
            step = self._next_target_step(target)
            if step is None:
                return f"Plan {target['plan_name']} is complete."
        needs = self._step_material_needs([step], target["ship_instance_id"])
        label = self._step_voice_label(
            step,
            self._step_loadout_match(step, target["steps"], target["ship_instance_id"]),
        )
        if not needs or not needs[0].engineering:
            return f"{label}: no engineering materials are required."
        need = needs[0]
        unknown = self._unknown_costs_note(needs)
        reference = _material_reference()
        inventory = self._material_inventory()
        rows = material_status(aggregate_needs([need]), inventory or {}, reference)
        if inventory is None:
            listing = "; ".join(f"{row.name}: {row.needed}" for row in rows)
            return f"Materials for {label}: {listing}. {MATERIALS_UNAVAILABLE}{unknown}"
        listing = "; ".join(
            f"{row.name}: need {row.needed}, have {row.have}"
            + (f", missing {row.missing}" if row.missing else "")
            for row in rows
        )
        missing = [row for row in rows if row.missing]
        verdict = (
            f"{len(missing)} material types are missing."
            if missing else "All materials are in stock."
        )
        return f"Materials for {label}: {listing}. {verdict}{unknown}"

    def _build_advice_action(
        self,
        args: BuildAdviceParams | None = None,
        context: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> str:
        """Full DAG route from the active session or latest imported goal.

        When `args.module` is supplied, only matching module steps are planned;
        otherwise the complete target build is planned.
        """
        if isinstance(args, str) and session_id is None:
            # Backward compatibility for direct tests/callers that passed a session id.
            session_id, args = args, None
        selected_module = args.module.strip() if args and args.module else None
        target = self._upgrade_target(session_id, context)
        if isinstance(target, str):
            return target
        target_steps = target["steps"]
        ship_instance_id = target["ship_instance_id"]
        ship_name = target["ship_name"]
        if selected_module:
            target_steps = [
                step for step in target_steps
                if self._step_matches_module_query(step, selected_module)
            ]
            if not target_steps:
                return f"The active target build has no module matching '{selected_module}'."
        loadout = self._loadout_for_ship(ship_instance_id)
        deltas = analyze_build(target_steps, loadout)
        # One ephemeral cache per advice calculation. It is discarded on
        # return, so station data is never persisted or reused across asks.
        station_request_cache: dict[str, dict[str, Any]] = {}

        def station_search(
            module_filters: list[dict[str, Any]],
            *,
            distance: int | float = 50000,
            size: int = 25,
            cache_key: str | None = None,
        ) -> dict[str, Any]:
            if self.helper is None:
                return {"status": "error", "error": "COVAS runtime helper unavailable", "stations": []}
            return self.helper.search_stations_for_modules(
                module_filters,
                distance=distance,
                size=size,
                cache=station_request_cache,
                cache_key=cache_key,
            )

        inventory = self._material_inventory()
        remaining_steps = [
            step for step in target_steps if step.get("id") not in target["completed"]
        ]
        trades = (
            self._planned_trades(remaining_steps, ship_instance_id, inventory)
            if inventory is not None else []
        )
        trader_search: Callable[[str], dict[str, Any]] | None = None
        helper = self.helper
        if trades and helper is not None and hasattr(helper, "search_material_traders"):
            def search_traders(category: str) -> dict[str, Any]:
                return helper.search_material_traders(category, cache=station_request_cache)

            trader_search = search_traders

        reference = _material_reference()
        engineer_progress = self._engineer_progress()
        actions = build_advice(
            deltas,
            self._catalog,
            self._engineer_registry,
            current_position=self._current_position,
            ship_name=ship_name,
            station_search=station_search,
            inventory=inventory,
            engineer_progress=engineer_progress,
            trades=trades,
            trader_search=trader_search,
            material_id_of=lambda cost: reference.resolve(
                cost.get("material_id") or cost.get("material_localised")
            ),
        )
        for action in actions:
            if action.kind == "experimental":
                action.target = _engineering_catalog_display_name(
                    action.target, "specials.json", "edname"
                )
        progress_note = ""
        if engineer_progress is None and any(action.kind == "engineer_visit" for action in actions):
            progress_note = (
                " Engineer unlock status is unknown: the game has not reported engineer "
                "progress yet, so engineers were chosen without checking access."
            )
        if args and args.detailed:
            route_text = (
                self._route_summary(actions, deltas, invite_detail=False) + " "
                + self._detailed_route(actions, ship_instance_id, deltas)
            )
        else:
            route_text = self._route_summary(actions, deltas)
        return route_text + progress_note + self._materials_overview(
            remaining_steps, ship_instance_id
        )

    def _route_summary(
        self, actions: list[PlannedAction], deltas: list[SlotDelta], *, invite_detail: bool = True
    ) -> str:
        pending = [delta for delta in deltas if delta.status != "installed"]
        if not pending:
            return "The build matches the plan; no modules remain."
        visits: list[str] = []
        for index, action in enumerate(actions):
            if action.kind != "engineer_visit":
                continue
            slots: set[str] = set()
            local_grades: dict[str, int] = {}
            for following in actions[index + 1:]:
                if following.kind in {"engineer_visit", "station_purchase", "material_trade"}:
                    break
                if following.kind in {"apply_blueprint", "experimental"} and following.slot:
                    slots.add(following.slot)
                if following.kind == "apply_blueprint" and following.slot and following.grade:
                    local_grades[following.slot] = max(
                        local_grades.get(following.slot, 0), following.grade
                    )
            labels = [
                self._pretty_module_name(delta.target_item)
                for delta in pending if delta.slot in slots
            ]
            visits.append(f"{action.target} in {action.system} for {', '.join(dict.fromkeys(labels))}"
                          + (f" (access: {action.access})" if action.access else "")
                          + "".join(
                              f" ({self._pretty_module_name(delta.target_item)}: grade "
                              f"{local_grades[delta.slot]} here, target grade {delta.target_level} "
                              "requires another engineer)"
                              for delta in pending
                              if delta.slot in local_grades and delta.target_level
                              and local_grades[delta.slot] < delta.target_level
                          ))
        count = len(pending)
        blockers = [action.target for action in actions if action.kind == "blocking"]
        prefix = "Partial route ready" if blockers else "Route ready"
        summary = f"{prefix} to upgrade {count} remaining module{'s' if count != 1 else ''}."
        if count <= 3:
            names = ", ".join(self._pretty_module_name(delta.target_item) for delta in pending)
            summary += f" Modules: {names}."
        if visits:
            summary += " Visit " + "; then ".join(visits) + "."
        if blockers:
            summary += " Limitations: " + "; ".join(blockers) + "."
        if invite_detail:
            summary += " Ask for the detailed route to hear all stops, grades, effects and estimated jumps."
        return summary

    def _detailed_route(
        self, actions: list[PlannedAction], ship_instance_id: str, deltas: list[SlotDelta]
    ) -> str:
        jump_range = self._maximum_jump_range_by_ship.get(str(ship_instance_id))
        names = {delta.slot: self._pretty_module_name(delta.target_item) for delta in deltas}
        parts = []
        for action in actions:
            if action.kind in {"station_purchase", "material_trade", "engineer_visit"}:
                leg = action.leg_distance_ly
                hops = (
                    f"at least {math.ceil(leg / jump_range)} estimated jumps"
                    if leg is not None and jump_range else "jumps unknown (distance or maximum jump range unavailable)"
                )
                detail = (
                    f"{action.system or 'unknown system'} / {action.station or action.target}: "
                    f"{action.target}; {leg if leg is not None else 'unknown'} ly from previous stop; {hops}"
                    + (f"; access: {action.access}" if action.access else "")
                )
                if action.kind == "material_trade" and action.materials:
                    detail += "; trade " + ", ".join(
                        f"{trade['give_quantity']} {trade['give_name']} for "
                        f"{trade['receive_quantity']} {trade['receive_name']}"
                        for trade in action.materials
                    )
                parts.append(detail)
            elif action.kind == "experimental":
                parts.append(
                    f"{names.get(action.slot, action.slot)}: experimental effect {action.target}"
                    + (" already installed" if action.stock_status == "installed" else " to apply")
                )
            elif action.kind == "apply_blueprint":
                blueprint = _engineering_catalog_display_name(
                    action.target, "blueprints.json", "fdname"
                )
                parts.append(f"{names.get(action.slot, action.slot)}: {blueprint} grade {action.grade}"
                             + (f" ({action.increments} increments)" if action.increments > 1 else "")
                             + (" (materials missing)" if action.stock_status == "missing" else "")
                             + (" (materials unverified)" if action.cost_status == "unknown" else ""))
            elif action.kind == "blocking":
                parts.append(f"Blocked: {action.target}")
            elif action.kind == "install_module":
                parts.append(f"Install {action.target} in {action.slot}")
        for delta in deltas:
            if delta.status != "installed" and delta.target_blueprint and not delta.target_experimental:
                parts.append(f"{names[delta.slot]}: no experimental effect specified by the plan")
        return (
            "Detailed stops (jump counts are straight-line lower-bound estimates, "
            "not a navigable route): " + "; then ".join(parts) + "."
            if parts else "Nothing pending — the build matches the plan."
        )

    def _engineer_progress(self) -> dict[str, dict[str, Any]] | None:
        """Engineer name -> journal EngineerProgress entry; None when the game
        has not reported engineer progress."""
        states = self._projected_states()
        if states is None:
            return None
        state = states.get("EngineerProgress")
        if isinstance(state, BaseModel):
            state = state.model_dump()
        if not isinstance(state, dict):
            return None
        progress: dict[str, dict[str, Any]] = {}
        for entry in state.get("Engineers") or []:
            if isinstance(entry, BaseModel):
                entry = entry.model_dump()
            if isinstance(entry, dict) and entry.get("Engineer"):
                progress[str(entry["Engineer"])] = entry
        return progress or None

    def _planned_trades(
        self, steps: list[dict[str, Any]], ship_instance_id: str, inventory: dict[str, int]
    ) -> list[PlannedTrade]:
        """Material trader exchanges from surplus stock covering the shortfalls
        of the steps; surplus spent by one exchange is not offered twice."""
        totals = aggregate_needs(self._step_material_needs(steps, ship_instance_id))
        if not totals:
            return []
        reference = _material_reference()
        stock = dict(inventory)
        trades: list[PlannedTrade] = []
        for row in shortfalls(totals, inventory, reference):
            category = reference.info(row.material_id).category or ""
            for option in trade_options(row.material_id, row.missing, stock, totals, reference):
                stock[option.material_id] = stock.get(option.material_id, 0) - option.give
                trades.append(
                    PlannedTrade(
                        category=category,
                        give_id=option.material_id,
                        give_name=option.name,
                        give_quantity=option.give,
                        receive_id=row.material_id,
                        receive_name=row.name,
                        receive_quantity=option.receive,
                    )
                )
        return trades

    def _preview_changes_action(
        self, args: PlanChangeParams, _context: dict[str, Any]
    ) -> str:
        normalized = parse_plan_input(args.plan_input)
        return self._format_plan_diff_text(
            self.diff_plan(normalized["plan_name"], normalized)
        )

    def _apply_changes_action(
        self, args: PlanChangeParams, _context: dict[str, Any]
    ) -> str:
        normalized = parse_plan_input(args.plan_input)
        diff = self.diff_plan(normalized["plan_name"], normalized)
        plan_id = self.import_plan(
            normalized["ship_model"],
            normalized["plan_name"],
            normalized,
        )
        if diff["current_version"] is None:
            return f"Applied new plan {normalized['plan_name']}."
        return (
            f"Applied {normalized['plan_name']} version {diff['next_version']}; "
            f"{diff['added_count']} added, {diff['removed_count']} removed, "
            f"{diff['changed_count']} changed. Plan id {plan_id}."
        )

    @staticmethod
    def _format_plan_diff_text(diff: dict[str, Any]) -> str:
        if diff["current_version"] is None:
            return f"New plan {diff['plan_name']}; all modules will be added."
        return (
            f"{diff['plan_name']} version {diff['current_version']} to "
            f"{diff['next_version']}: {diff['added_count']} added, "
            f"{diff['removed_count']} removed, {diff['changed_count']} changed."
        )

    def _import_from_settings(self) -> None:
        """Parse the pasted plan data and store it directly.

        The plan is filed under its ship model; no diff is computed here —
        comparing a plan against the current ship only makes sense once the
        app stores per-ship data (deferred)."""
        value = self.settings.get("plan_input", "")
        try:
            normalized = parse_plan_input(value)
            plan_id = self.import_plan(
                normalized["ship_model"], normalized["plan_name"], normalized
            )
        except (PlanParseError, ValueError, TypeError, json.JSONDecodeError) as error:
            log("error", f"Ship Upgrade Manager plan import failed: {error}")
            self._enter_import_state(
                IMPORT_STATE_DATA, error=("plugin.sum.errParse", {"detail": str(error)})
            )
            return
        with self.get_db() as db:
            row = db.execute(
                "SELECT plan_version FROM plans WHERE id = ?", (plan_id,)
            ).fetchone()
            sessions = db.execute(
                "SELECT COUNT(*) FROM active_session WHERE plan_id = ?", (plan_id,)
            ).fetchone()[0]
        record = {
            "plan_id": plan_id,
            "plan_name": normalized["plan_name"],
            "ship_model": normalized["ship_model"],
            "version": row["plan_version"] if row else 1,
            "modules": len(normalized["steps"]),
            "sessions": sessions,
        }
        self._save_last_import(record)
        self._last_import = record
        self._publish_status()
        self._enter_import_state(IMPORT_STATE_IMPORTED)
        log("info", f"Imported Ship Upgrade Manager plan {plan_id} version {record['version']}")

    def _plan_status_action(self) -> str:
        """Report loaded-target and active-session status without ambiguity."""
        session = self.get_session()
        if session is not None:
            ship = session.get("ship_custom_name") or self._ship_display_name(
                session.get("ship_model", "")
            ) or session["ship_instance_id"]
            return (
                f"An upgrade plan is active and in progress: {session['plan_name']} "
                f"for {ship}, with {len(session['completed_steps'])}/"
                f"{session['total_steps']} tracked steps complete."
            )

        last_import = self._load_last_import()
        with self.get_db() as db:
            if last_import and last_import.get("plan_id"):
                row = db.execute(
                    "SELECT plan_name, ship_model FROM plans WHERE id = ?",
                    (last_import["plan_id"],),
                ).fetchone()
            else:
                row = db.execute(
                    "SELECT plan_name, ship_model FROM plans ORDER BY last_modified DESC LIMIT 1"
                ).fetchone()
        if row is None:
            return "No ship-upgrade plan is loaded. Import a target build in Plans settings."
        ship = self._ship_display_name(row["ship_model"])
        return (
            f"A target upgrade plan is loaded but not active: {row['plan_name']} "
            f"for {ship}. Activate it with the plan's play button to track an in-game session."
        )

    def _current_loadout_action(
        self, args: LoadoutQueryParams, _context: dict[str, Any]
    ) -> str:
        """Report installed hardware directly from the current loadout projection."""
        states = self._projected_states()
        if states is None:
            return "The current ship loadout is unavailable outside runtime."
        loadout = states.get("Loadout")
        if isinstance(loadout, BaseModel):
            loadout = loadout.model_dump()
        if not isinstance(loadout, dict):
            return "The current ship loadout is unavailable."
        modules = loadout.get("Modules")
        if not isinstance(modules, list) or not modules:
            return "The current ship loadout has no module details yet."
        requested_slot = (args.slot or "").strip().casefold()
        if requested_slot:
            matching = [
                module for module in modules
                if isinstance(module, dict)
                and str(module.get("Slot") or "").casefold() == requested_slot
            ]
            if not matching:
                return f"No installed module was found in the {args.slot} slot."
        else:
            matching = [module for module in modules if isinstance(module, dict)]
        descriptions: list[str] = []
        for module in matching:
            item_id = str(module.get("Item") or "").strip()
            slot = str(module.get("Slot") or "").strip()
            if not item_id:
                continue
            display, _key, _grade = self._module_display(item_id)
            details = f"{slot}: {display} (technical id: {item_id})"
            engineering = module.get("Engineering")
            if isinstance(engineering, dict):
                blueprint = str(engineering.get("BlueprintName") or "").strip()
                level = engineering.get("Level")
                if blueprint:
                    details += f", engineered with {_blueprint_family_display(blueprint)}"
                if level is not None:
                    details += f" grade {int(float(level))}"
            descriptions.append(details)
        ship_id = str(loadout.get("Ship") or "").strip()
        ship_name = self._ship_display_name(ship_id) if ship_id else "the current ship"
        if not descriptions:
            return f"No installed module details are available for {ship_name}."
        return f"Installed modules on {ship_name}: " + "; ".join(descriptions) + "."

    def _state_query_action(self, args: StateQueryParams, _context: dict[str, Any]) -> str:
        """Detailed state report for the AI (spec intents.state.query)."""
        session = self.get_session(None, {"ship_instance_id": args.ship} if args.ship else None)
        if session is None:
            ship = args.ship or "the current ship"
            return f"No active ship upgrade session for {ship}."
        progress = self._plan_progress_for_session(session)
        parts = [
            f"Plan {session['plan_name']} on {progress.get('ship', session['ship_custom_name'] or session['ship_instance_id'])}",
            f"{progress.get('modules_done', 0)}/{progress.get('modules_total', 0)} modules installed ({progress.get('modules_pct', 0)}%)",
        ]
        steps = session.get("steps") or []
        if steps:
            parts.append(
                self._missing_modules_summary(
                    steps, self._loadout_for_ship(str(session["ship_instance_id"]))
                ).rstrip(".")
            )
        if progress.get("eng_target"):
            parts.append(
                f"engineering {progress.get('eng_current', 0)}/{progress.get('eng_target')} grade levels ({progress.get('eng_pct', 0)}%)"
            )
        next_label = progress.get("next_label")
        if next_label:
            next_class = progress.get("next_class", "")
            parts.append(f"next module: {next_label} {next_class}".strip())
        if session.get("paused"):
            parts.append("session paused")
        return ". ".join(parts) + "."

    def _plan_progress_for_session(self, session: dict[str, Any]) -> ListRowProgress:
        plan_id = session.get("plan_id") or ""
        with self.get_db() as db:
            row = db.execute("SELECT ship_model FROM plans WHERE id = ?", (plan_id,)).fetchone()
        ship_model = row["ship_model"] if row else ""
        progress_list = self._plan_progress(
            plan_id, ship_model, str(session.get("ship_instance_id") or "")
        )
        return progress_list[0] if progress_list else {}

    def _goal_declare_action(self, args: GoalParams, _context: dict[str, Any]) -> str:
        """Activate a plan as the current build goal (spec intents.goal.declare)."""
        ship_id = self._resolve_current_ship_id()
        if not ship_id:
            return "No ship detected from the journal — cannot start a build goal."
        existing = next(
            (
                session for session in self.list_sessions()
                if session["plan_name"] == args.plan_name
                and str(session["ship_instance_id"]) == str(ship_id)
            ),
            None,
        )
        if existing is not None:
            # Restarting would discard progress already tracked for this goal.
            return (
                f"Build goal already active: {existing['plan_name']} on "
                f"{existing['ship_custom_name'] or existing['ship_instance_id']} "
                f"({existing['total_steps']} steps, "
                f"{len(existing['completed_steps'])} already done)."
            )
        try:
            session = self.start_session(args.plan_name, ship_id, self._current_ship_name)
        except ValueError as error:
            return f"Cannot activate plan '{args.plan_name}': {error}"
        return (
            f"Build goal set: {session['plan_name']} on "
            f"{session['ship_custom_name'] or session['ship_instance_id']} "
            f"({session['total_steps']} steps, "
            f"{len(session['completed_steps'])} already done)."
        )

    def _delete_plan_by_id(self, plan_id: str) -> None:
        try:
            plan = self.get_plan(plan_id)
            with self.get_db() as db:
                db.execute("DELETE FROM plans WHERE id = ?", (plan_id,))
        except ValueError as error:
            log("error", f"Ship Upgrade Manager plan deletion failed: {error}")
            self._set_message_field(
                "plans", "plans_status", "plugin.sum.errDelete", {"detail": str(error)}
            )
            return
        if self._last_import and self._last_import.get("plan_id") == plan_id:
            self._clear_last_import()
            self._enter_import_state(IMPORT_STATE_IDLE)
        self._set_message_field(
            "plans", "plans_status", "plugin.sum.msg.deleted", {"plan": plan["plan_name"]}
        )
        self._publish_status()

    def _detect_ship_from_journal(self) -> dict[str, str]:
        """Best-effort read of the journals to find the current ship id and name.

        Journal side effects only reach the plugin once the runtime is started,
        so the settings UI relies on this direct read in config state. Journals
        are scanned from the newest file backwards: the first ship id found is
        the most recently recorded one."""
        try:
            log_files = self.journal_files()
        except (OSError, FileNotFoundError) as error:
            log("warn", f"Ship Upgrade Manager journal detection failed: {error}")
            return {}
        for log_file in sorted(log_files, key=os.path.getmtime, reverse=True):
            try:
                with open(log_file, encoding="utf-8", errors="ignore") as handle:
                    lines = handle.readlines()
            except OSError as error:
                log("warn", f"Ship Upgrade Manager journal read failed: {error}")
                continue
            for line in reversed(lines):
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict):
                    continue
                detected = self._ship_identity(entry)
                if detected:
                    return detected
        return {}

    # Journal events that identify the ship the commander is currently flying.
    # ShipyardTransfer/ShipyardSell also carry ship ids, but of other ships.
    _SHIP_IDENTITY_EVENTS: ClassVar[frozenset[str]] = frozenset({
        "LoadGame",
        "Loadout",
        "ShipyardSwap",
        "ShipyardNew",
        "SetUserShipName",
        "ModuleBuy",
        "ModuleSell",
        "ModuleSwap",
        "ModuleStore",
        "ModuleRetrieve",
        "MassModuleStore",
        "FetchRemoteModule",
    })

    @classmethod
    def _ship_identity(cls, entry: dict[str, Any]) -> dict[str, str]:
        """Current ship {id, name?, type?} carried by a journal entry.

        The ship instance id (journal ShipID) is the only key of a ship: the
        type is shared by every ship of the same model and names are
        player-defined, so neither may identify the upgrade plan in progress."""
        event_name = entry.get("event")
        if event_name not in cls._SHIP_IDENTITY_EVENTS:
            return {}
        ship_id = entry.get("NewShipID") if event_name == "ShipyardNew" else entry.get("ShipID")
        if ship_id is None or isinstance(ship_id, bool) or not str(ship_id).strip():
            return {}
        ship_type = str(entry.get("ShipType") or entry.get("Ship") or "").strip()
        lowered_type = ship_type.casefold()
        # On foot or in an Apex taxi, LoadGame reports the suit/taxi instead.
        if "suit" in lowered_type or lowered_type.endswith("_taxi"):
            return {}
        identity = {"id": str(ship_id).strip()}
        name = entry.get("UserShipName" if event_name == "SetUserShipName" else "ShipName")
        if isinstance(name, str) and name.strip():
            identity["name"] = name.strip()
        if ship_type:
            identity["type"] = ship_type
        return identity

    def _set_current_ship(self, identity: dict[str, str]) -> bool:
        """Switch the tracked current ship; True when the ship changed.

        The live loadout snapshot belongs to the previous ship, so it is
        dropped on a change; the new ship's modules are then read from its
        own Loadout (journal baseline) until a fresh snapshot arrives."""
        ship_id = str(identity.get("id") or "").strip()
        if not ship_id:
            return False
        changed = ship_id != str(self._current_ship_id)
        if changed:
            self._current_ship_id = ship_id
            self._current_ship_name = ""
            self._current_loadout_modules = []
        name = identity.get("name")
        if name:
            self._current_ship_name = name
            with self.get_db() as db:
                db.execute(
                    """
                    UPDATE active_session SET ship_custom_name = ?
                    WHERE ship_instance_id = ? AND COALESCE(ship_custom_name, '') != ?
                    """,
                    (name, ship_id, name),
                )
        return changed

    def _resolve_current_ship_id(self) -> str:
        """Current ship instance id, detected from the journals when no
        journal event has identified it yet."""
        if not self._current_ship_id:
            detected = self._detect_ship_from_journal()
            if detected:
                self._set_current_ship(detected)
        return self._current_ship_id

    def _rename_plan_from_settings(self, plan_id: str, new_name: str) -> None:
        try:
            self.rename_plan(plan_id, new_name)
        except ValueError as error:
            log("error", f"Ship Upgrade Manager plan rename failed: {error}")
            self._set_message_field(
                "plans", "plans_status", "plugin.sum.errRename", {"detail": str(error)}
            )

    def _start_plan_session_from_settings(self, plan_id: str) -> None:
        if not self._resolve_current_ship_id():
            self._set_message_field("plans", "plans_status", "plugin.sum.errNoShip")
            return
        try:
            plan = self.get_plan(plan_id)
            self.start_session(plan["plan_name"], self._current_ship_id, self._current_ship_name)
        except ValueError as error:
            log("error", f"Ship Upgrade Manager session start failed: {error}")
            self._set_message_field(
                "plans", "plans_status", "plugin.sum.errStart", {"detail": str(error)}
            )
            return
        self._set_message_field(
            "plans",
            "plans_status",
            "plugin.sum.msg.sessionStarted",
            {"plan": plan["plan_name"]},
        )

    def rename_plan(self, plan_id: str, new_name: str) -> bool:
        """Rename a plan in place, keeping its stable plan id."""
        if not new_name or not new_name.strip():
            raise ValueError("a new plan name is required")
        with self.get_db() as db:
            row = db.execute(
                "SELECT plan_name FROM plans WHERE id = ?", (plan_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"Unknown plan: {plan_id}")
            try:
                db.execute(
                    "UPDATE plans SET plan_name = ? WHERE id = ?",
                    (new_name.strip(), plan_id),
                )
            except sqlite3.IntegrityError as error:
                raise ValueError(
                    f"A plan named '{new_name.strip()}' already exists for this ship"
                ) from error
        old_name = row["plan_name"]
        if self._last_import and self._last_import.get("plan_id") == plan_id:
            self._last_import["plan_name"] = new_name.strip()
            self._save_last_import(self._last_import)
            self._enter_import_state(self._import_state)
        self._set_message_field(
            "plans", "plans_status", "plugin.sum.msg.renamed", {"name": new_name.strip()}
        )
        log("info", f"Renamed Ship Upgrade Manager plan {plan_id} from {old_name}")
        self._publish_status()
        return True

    def _on_event(self, event: Event[Any], _context: dict[str, Any]) -> None:
        if not isinstance(event, GameEvent):
            return
        self._handle_ship_event(dict(event.content), announce=not event.historic)

    def handle_watched_journal_entry(self, entry: dict[str, Any]) -> None:
        """Journal entry tailed by the watcher; dropped while the runtime
        delivers the same events through the EventManager."""
        if self._watch_active:
            self._handle_ship_event(entry)

    def _announce_upgrade(self, message: str) -> None:
        if self.helper is not None:
            self.helper.dispatch_event(PluginEvent(
                plugin_event_name="ShipUpgradeNotice",
                plugin_event_content=message,
            ))

    def _handle_ship_event(self, content: dict[str, Any], *, announce: bool = True) -> None:
        event_name = content.get("event")
        # Every plan in progress is keyed by the journal ShipID: follow the
        # ship the commander flies so the matching plan is resumed on a swap.
        identity = self._ship_identity(content)
        ship_changed = self._set_current_ship(identity) if identity else False
        ship_id = identity.get("id") or (
            self._resolve_current_ship_id()
            if event_name in {"EngineerCraft", "Loadout", "ModuleInfo", "ModuleBuy",
                              "ModuleSwap", "ModuleStore", "ModuleRetrieve"} else ""
        )
        range_notice: str | None = None
        if event_name == "Loadout" and identity.get("id"):
            reported = content.get("MaxJumpRange")
            if isinstance(reported, (int, float)) and not isinstance(reported, bool) and math.isfinite(reported) and reported > 0:
                previous = self._range_notice_baseline_by_ship.setdefault(ship_id, float(reported))
                self._maximum_jump_range_by_ship[ship_id] = float(reported)
                if abs(previous - reported) >= 0.1 - 1e-9:
                    self._range_notice_baseline_by_ship[ship_id] = float(reported)
                    range_notice = (
                        f"The maximum jump range of this ship changed from {previous:.1f} "
                        f"to {reported:.1f} ly. Would you like me to recalculate the upgrade route? "
                        "I have not recalculated it yet."
                    )
        if event_name == "LoadGame":
            fid = content.get("FID")
            if fid:
                self._current_fid = str(fid)
            if identity:
                self._publish_status()
            return
        if event_name in {"ShipyardSwap", "ShipyardNew", "SetUserShipName"}:
            if identity:
                self._publish_status()
            return
        if event_name in {"Location", "FSDJump", "CarrierJump"}:
            system = content.get("StarSystem")
            if isinstance(system, str):
                self._current_system = system
            self._current_workshop = self._engineer_at_workshop(
                self._current_system, content.get("StationName")
            ) if event_name == "Location" and content.get("Docked") else None
            star_pos = content.get("StarPos")
            if isinstance(star_pos, list) and len(star_pos) == 3:
                try:
                    x, y, z = (float(value) for value in star_pos)
                except (TypeError, ValueError):
                    pass
                else:
                    self._current_position = (x, y, z)
            return
        if event_name == "Docked":
            system = content.get("StarSystem")
            if isinstance(system, str):
                self._current_system = system
            self._current_workshop = self._engineer_at_workshop(
                self._current_system, content.get("StationName")
            )
            return
        if event_name == "Undocked":
            self._current_workshop = None
            return
        if event_name == "EngineerCraft":
            # The game does not write a Loadout after each craft (often not
            # until the player leaves the engineer), so the craft itself
            # carries the new engineering state of the module.
            blueprint = content.get("BlueprintName") or ""
            level = content.get("Level")
            log("info", f"EngineerCraft: {blueprint} grade {level}")
            if ship_id:
                self._current_loadout_modules = self._apply_engineer_craft(
                    self._loadout_for_ship(ship_id), content
                )
        elif event_name not in {
            "Loadout",
            "ModuleInfo",
            "ModuleBuy",
            "ModuleSwap",
            "ModuleStore",
            "ModuleRetrieve",
        }:
            if ship_changed:
                self._publish_status()
            return
        else:
            # ModuleInfo carries no ShipID: it always describes the current ship.
            if event_name in {"Loadout", "ModuleInfo"}:
                loadout_modules = content.get("Modules")
                if isinstance(loadout_modules, list):
                    new_modules = [
                        module for module in loadout_modules if isinstance(module, dict)
                    ]
                    if event_name == "ModuleInfo":
                        new_modules = self._carry_over_engineering(
                            self._current_loadout_modules, new_modules
                        )
                    self._current_loadout_modules = new_modules
        if event_name in {"Loadout", "ModuleInfo", "EngineerCraft"} and ship_id:
            modules = [
                {"item": module.get("Item"), "slot": module.get("Slot"),
                 "engineering": module.get("Engineering")}
                for module in self._current_loadout_modules
            ]
        else:
            modules = self._event_modules(content)
        with self.get_db() as db:
            rows = db.execute("SELECT id, ship_instance_id FROM active_session").fetchall()
        if not ship_id and len({str(row["ship_instance_id"]) for row in rows}) > 1:
            log("warn",
                "Skipping ship upgrade detection because the event has no ship identifier "
                "and multiple ships have active sessions",
            )
            return
        notices: list[str] = []
        for row in rows:
            session = self.get_session(row["id"])
            if session is None or session["paused"]:
                continue
            if ship_id and str(session["ship_instance_id"]) != str(ship_id):
                continue
            remaining_steps = [
                step for step in session["steps"]
                if step["id"] not in session["completed_steps"]
            ]
            for step_id in self._satisfied_step_ids(remaining_steps, modules):
                step = next(item for item in remaining_steps if item["id"] == step_id)
                try:
                    self.complete_step(
                        step_id, "Detected from Loadout event", session["id"]
                    )
                except ValueError as error:
                    log("warn", f"Could not auto-complete ship upgrade step: {error}")
                    continue
                if announce and self.helper is not None:
                    updated = self.get_session(session["id"])
                    if updated:
                        remaining = [
                            item for item in updated["steps"]
                            if item["id"] not in updated["completed_steps"]
                        ]
                        next_step = remaining[0] if remaining else None
                        engineer = self._workshop_engineer()
                        if engineer and remaining:
                            available, _limited = self._workshop_opportunities(
                                updated, engineer
                            )
                            if available:
                                next_step = available[0][0]
                        notice = f"{self._step_short_label(step)} has reached its plan target."
                        if next_step:
                            match = self._step_loadout_match(
                                next_step, updated["steps"], str(updated["ship_instance_id"])
                            )
                            notice += f" Next: {self._step_voice_label(next_step, match)}."
                        else:
                            notice += " The ship upgrade plan is complete."
                        notices.append(notice)
        # The progress bars reflect the ship's current loadout, so refresh
        # whenever a relevant journal event changes that state.
        self._publish_status()
        if range_notice and any(
            str(row["ship_instance_id"]) == str(ship_id)
            and (session := self.get_session(row["id"])) is not None
            and not session["paused"]
            for row in rows
        ):
            notices.append(range_notice)
        if announce and self.helper is not None and notices:
            self._announce_upgrade(" ".join(notices))

    def _engineer_at_workshop(self, system: str, station: Any) -> str | None:
        if not system or not isinstance(station, str):
            return None
        for engineer in self._engineer_registry.values():
            if (
                str(engineer.get("Location") or "").casefold() == system.casefold()
                and str(engineer.get("Workshop") or "").casefold() == station.casefold()
            ):
                return str(engineer["Engineer"])
        return None

    @staticmethod
    def _event_modules(content: dict[str, Any]) -> list[dict[str, Any]]:
        event_name = content.get("event")
        if event_name in {"Loadout", "ModuleInfo"}:
            modules = content.get("Modules", [])
            return [
                {
                    "item": module.get("Item"),
                    "slot": module.get("Slot"),
                    "engineering": module.get("Engineering"),
                }
                for module in modules
                if isinstance(module, dict)
            ] if isinstance(modules, list) else []
        if event_name == "ModuleBuy":
            return [
                {
                    "item": ShipUpgradeManagerPlugin._normalize_event_item(
                        content.get("BuyItem")
                    ),
                    "slot": content.get("Slot"),
                }
            ]
        if event_name == "ModuleSwap":
            item = content.get("ToItem")
            return [
                {
                    "item": ShipUpgradeManagerPlugin._normalize_event_item(item),
                    "slot": content.get("ToSlot"),
                }
            ]
        if event_name == "ModuleStore":
            return [
                {
                    "item": ShipUpgradeManagerPlugin._normalize_event_item(
                        content.get("StoredItem")
                    ),
                    "slot": content.get("Slot"),
                }
            ]
        if event_name == "ModuleRetrieve":
            return [
                {
                    "item": ShipUpgradeManagerPlugin._normalize_event_item(
                        content.get("RetrievedItem")
                    ),
                    "slot": content.get("Slot"),
                }
            ]
        if event_name == "EngineerCraft":
            return [
                {
                    "item": ShipUpgradeManagerPlugin._normalize_event_item(
                        content.get("Module")
                    ),
                    "slot": content.get("Slot"),
                    "engineering": ShipUpgradeManagerPlugin._engineer_craft_engineering(
                        content
                    ),
                }
            ]
        return []

    @staticmethod
    def _engineer_craft_engineering(content: dict[str, Any]) -> dict[str, Any]:
        """Loadout-shaped Engineering block from an EngineerCraft event."""
        engineering = {
            key: content[key]
            for key in (
                "Engineer",
                "EngineerID",
                "BlueprintID",
                "BlueprintName",
                "Level",
                "Quality",
                "ExperimentalEffect",
                "ExperimentalEffect_Localised",
                "Modifiers",
            )
            if key in content
        }
        if not engineering.get("ExperimentalEffect") and content.get("ApplyExperimentalEffect"):
            engineering["ExperimentalEffect"] = content["ApplyExperimentalEffect"]
        return engineering

    @classmethod
    def _apply_engineer_craft(
        cls, modules: list[dict[str, Any]], content: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Copy of a loadout module list with an EngineerCraft result applied
        to the crafted slot (unchanged when the slot/module does not match)."""
        slot = str(content.get("Slot") or "").casefold()
        item = cls._normalize_event_item(content.get("Module"))
        if not slot:
            return modules
        updated: list[dict[str, Any]] = []
        for module in modules:
            if (
                str(module.get("Slot") or "").casefold() == slot
                and (
                    not item
                    or canonical_item(str(module.get("Item") or "")) == canonical_item(item)
                )
            ):
                engineering = cls._engineer_craft_engineering(content)
                previous = module.get("Engineering") or {}
                if (
                    not engineering.get("ExperimentalEffect")
                    and previous.get("ExperimentalEffect")
                    and str(previous.get("BlueprintName") or "").casefold()
                    == str(engineering.get("BlueprintName") or "").casefold()
                ):
                    engineering["ExperimentalEffect"] = previous["ExperimentalEffect"]
                module = {**module, "Engineering": engineering}
            updated.append(module)
        return updated

    @staticmethod
    def _carry_over_engineering(
        previous: list[dict[str, Any]], modules: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """ModulesInfo snapshots omit Engineering: keep the known engineering
        of modules that are still fitted in the same slot."""
        known = {
            (str(module.get("Slot") or "").casefold(), canonical_item(str(module.get("Item") or ""))):
                module["Engineering"]
            for module in previous
            if module.get("Engineering")
        }
        merged: list[dict[str, Any]] = []
        for module in modules:
            key = (str(module.get("Slot") or "").casefold(), canonical_item(str(module.get("Item") or "")))
            if "Engineering" not in module and key in known:
                module = {**module, "Engineering": known[key]}
            merged.append(module)
        return merged

    @staticmethod
    def _experimental_effect(engineering: dict[str, Any]) -> str:
        return str(
            engineering.get("ExperimentalEffect")
            or engineering.get("experimental_effect")
            or engineering.get("experimentalEffect")
            or ""
        )

    @staticmethod
    def _normalize_event_item(item: Any) -> str | None:
        if not isinstance(item, str) or not item or item == "Null":
            return None
        if item.startswith("$") and item.endswith("_name;"):
            return item[1:-6]
        return item

    @staticmethod
    def _module_matches_step(step: dict[str, Any], module: dict[str, Any]) -> bool:
        item = step.get("item")
        slot = step.get("slot")
        module_item = module.get("item")
        if not isinstance(item, str) or not item or not isinstance(module_item, str):
            return False
        if canonical_item(module_item) != canonical_item(item) or (slot and module.get("slot") != slot):
            return False
        expected_engineering = step.get("engineering")
        if not expected_engineering:
            return True
        actual_engineering = module.get("engineering")
        if not isinstance(actual_engineering, dict):
            return False
        expected_blueprint = expected_engineering.get("BlueprintName")
        if expected_blueprint is not None and str(
            actual_engineering.get("BlueprintName") or ""
        ).casefold() != str(expected_blueprint).casefold():
            return False
        expected_level = expected_engineering.get("Level")
        if expected_level is not None and not grade_reached(
            actual_engineering.get("Level"), actual_engineering.get("Quality"), expected_level
        ):
            return False
        expected_effect = ShipUpgradeManagerPlugin._experimental_effect(expected_engineering)
        if expected_effect and ShipUpgradeManagerPlugin._experimental_effect(
            actual_engineering
        ).casefold() != expected_effect.casefold():
            return False
        return True

    def _satisfied_step_ids(
        self, steps: list[dict[str, Any]], modules: list[dict[str, Any]]
    ) -> list[str]:
        """Find satisfied plan steps with a one-to-one module assignment."""
        assigned_modules: dict[int, str] = {}
        steps_by_id = {str(step["id"]): step for step in steps}

        def assign(step_id: str, visited_modules: set[int]) -> bool:
            step = steps_by_id[step_id]
            for index, module in enumerate(modules):
                if index in visited_modules or not self._module_matches_step(step, module):
                    continue
                visited_modules.add(index)
                previous_step_id = assigned_modules.get(index)
                if previous_step_id is None or assign(previous_step_id, visited_modules):
                    assigned_modules[index] = step_id
                    return True
            return False

        for step in steps:
            step_id = str(step["id"])
            assign(step_id, set())

        matched_steps = set(assigned_modules.values())
        return [
            str(step["id"]) for step in steps if str(step["id"]) in matched_steps
        ]

    def _status_generator(self, _states: dict[str, BaseModel]) -> list[tuple[str, Any]]:
        session = self.get_session()
        if session is not None:
            remaining = [
                step for step in session["steps"]
                if step["id"] not in session["completed_steps"]
            ]
            next_step = (
                self._step_voice_label(
                    remaining[0],
                    self._step_loadout_match(
                        remaining[0], session["steps"], str(session["ship_instance_id"])
                    ),
                )
                if remaining else "complete"
            )
            engineer = self._workshop_engineer(_states)
            if engineer and remaining:
                available, _limited = self._workshop_opportunities(session, engineer)
                loadout = self._loadout_for_ship(str(session["ship_instance_id"]))
                progress = (self._engineer_progress() or {}).get(engineer, {})
                next_step = (
                    f"at {engineer}, planned upgrades supported here: "
                    + ", ".join(
                        self._workshop_planned_offer(step, grade, loadout)
                        for step, grade in available
                    )
                    + (
                        " (engineer rank or unlock status unreported)"
                        if available and (
                            progress.get("Progress") != "Unlocked"
                            or not isinstance(progress.get("Rank"), int)
                        )
                        else ""
                    )
                    if available
                    else f"at {engineer}, no remaining planned upgrade is available here"
                )
            ship = session["ship_custom_name"] or self._ship_display_name(
                session.get("ship_model", "")
            ) or session["ship_instance_id"]
            missing_modules = self._missing_modules_summary(
                session["steps"], self._loadout_for_ship(str(session["ship_instance_id"]))
            )
            return [
                (
                    "Ship upgrade",
                    f"{session['plan_name']} on {ship}: "
                    f"{len(session['completed_steps'])}/{session['total_steps']} steps tracked; "
                    f"next: {next_step}. {missing_modules}",
                )
            ]

        # An imported target build is a valid planning goal even before the
        # player activates progress tracking with the UI/voice goal action.
        last_import = self._load_last_import()
        with self.get_db() as db:
            if last_import and last_import.get("plan_id"):
                plan_row = db.execute(
                    "SELECT id, plan_name, ship_model, source_json FROM plans WHERE id = ?",
                    (last_import["plan_id"],),
                ).fetchone()
            else:
                plan_row = db.execute(
                    "SELECT id, plan_name, ship_model, source_json FROM plans "
                    "ORDER BY last_modified DESC LIMIT 1"
                ).fetchone()
        if plan_row is None:
            return []
        plan = dict(plan_row)
        ship_id = self._resolve_current_ship_id()
        if not ship_id:
            return [("Ship upgrade target", f"Target build {plan['plan_name']} is loaded; current ship not identified yet.")]
        steps = self._plan_steps(plan["source_json"])
        loadout = self._loadout_for_ship(ship_id)
        metrics = self._progress_metrics(steps, loadout)
        ship = self._current_ship_name or self._ship_display_name(plan["ship_model"])
        next_step = next(
            (step for step in steps if not self._step_satisfied_by_loadout(step, loadout)),
            None,
        )
        next_label = (
            self._step_voice_label(
                next_step, self._step_loadout_match(next_step, steps, ship_id)
            )
            if next_step else "complete"
        )
        engineering = (
            f"; engineering {metrics['eng_current']}/{metrics['eng_target']} levels"
            if metrics["eng_target"] else ""
        )
        return [
            (
                "Ship upgrade target",
                f"Target build {plan['plan_name']} for {ship}; no progress session has been activated. "
                f"Current loadout matches {metrics['modules_done']}/{metrics['modules_total']} target modules "
                f"({metrics['modules_pct']}%){engineering}; suggested first step: {next_label}. "
                + self._missing_modules_summary(steps, loadout),
            )
        ]

    def _step_satisfied_by_loadout(self, step: dict[str, Any], loadout: list[dict[str, Any]]) -> bool:
        return bool(self._steps_satisfied_by_loadout([step], loadout))

    def get_db_path(self) -> Path:
        if self.helper is not None:
            data_path = self.helper.get_plugin_data_path(self.plugin_manifest)
        else:
            data_path = os.path.abspath(
                os.path.join(
                    PluginHelper.PLUGIN_DATA_PATH,
                    self.plugin_manifest.guid,
                )
            )
            os.makedirs(data_path, exist_ok=True)
        return Path(data_path) / "ship_upgrade_manager.db"

    @contextmanager
    def get_db(self) -> Generator[sqlite3.Connection]:
        connection = sqlite3.connect(self.get_db_path())
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize_database(self) -> None:
        with self.get_db() as db:
            # The first released schema used a singleton row (id='active').  Rebuild
            # it in place so old databases retain their session and history while
            # allowing arbitrary session ids.
            legacy = db.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='active_session'"
            ).fetchone()
            if legacy and legacy["sql"] and "CHECK (ID = 'ACTIVE')" in legacy["sql"].upper():
                legacy_columns = {
                    row["name"]
                    for row in db.execute("PRAGMA table_info(active_session)").fetchall()
                }
                paused_column = "paused" if "paused" in legacy_columns else "0"
                db.execute("PRAGMA foreign_keys = OFF")
                db.executescript(
                    """
                    CREATE TABLE active_session_v2 (
                        id TEXT PRIMARY KEY,
                        plan_id TEXT NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
                        ship_instance_id TEXT NOT NULL,
                        ship_custom_name TEXT,
                        current_step INTEGER NOT NULL DEFAULT 0,
                        completed_steps TEXT NOT NULL DEFAULT '[]',
                        session_start TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        last_activity TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        plan_version_at_session_start INTEGER NOT NULL,
                        paused INTEGER NOT NULL DEFAULT 0
                    );
                    INSERT INTO active_session_v2
                        SELECT id, plan_id, ship_instance_id, ship_custom_name,
                               current_step, completed_steps, session_start,
                               last_activity, plan_version_at_session_start,
                               {paused_column}
                        FROM active_session;
                    CREATE TABLE step_history_v2 (
                        id TEXT PRIMARY KEY,
                        plan_session_id TEXT NOT NULL,
                        step_id TEXT NOT NULL,
                        completed_at TEXT NOT NULL,
                        plan_version_at_completion INTEGER NOT NULL,
                        notes TEXT,
                        UNIQUE(plan_session_id, step_id)
                    );
                    INSERT INTO step_history_v2 SELECT * FROM step_history;
                    DROP TABLE step_history;
                    DROP TABLE active_session;
                    ALTER TABLE active_session_v2 RENAME TO active_session;
                    ALTER TABLE step_history_v2 RENAME TO step_history;
                    """.format(paused_column=paused_column)
                )
                db.execute("PRAGMA foreign_keys = ON")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS plans (
                    id TEXT PRIMARY KEY,
                    ship_model TEXT NOT NULL,
                    plan_name TEXT NOT NULL,
                    plan_version INTEGER NOT NULL DEFAULT 1,
                    source_json TEXT,
                    source_hash TEXT,
                    import_date TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    last_modified TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(ship_model, plan_name)
                );

                CREATE TABLE IF NOT EXISTS plan_applications (
                    id TEXT PRIMARY KEY,
                    plan_id TEXT NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
                    ship_instance_id TEXT NOT NULL,
                    ship_custom_name TEXT,
                    applied_date TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    plan_version_applied INTEGER NOT NULL,
                    UNIQUE(plan_id, ship_instance_id)
                );

                CREATE TABLE IF NOT EXISTS active_session (
                    id TEXT PRIMARY KEY,
                    plan_id TEXT NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
                    ship_instance_id TEXT NOT NULL,
                    ship_custom_name TEXT,
                    current_step INTEGER NOT NULL DEFAULT 0,
                    completed_steps TEXT NOT NULL DEFAULT '[]',
                    session_start TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    last_activity TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    plan_version_at_session_start INTEGER NOT NULL,
                    paused INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS step_history (
                    id TEXT PRIMARY KEY,
                    plan_session_id TEXT NOT NULL,
                    step_id TEXT NOT NULL,
                    completed_at TEXT NOT NULL,
                    plan_version_at_completion INTEGER NOT NULL,
                    notes TEXT,
                    UNIQUE(plan_session_id, step_id)
                );

                CREATE TABLE IF NOT EXISTS session_history (
                    id TEXT PRIMARY KEY,
                    plan_id TEXT NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
                    ship_instance_id TEXT NOT NULL,
                    ship_custom_name TEXT,
                    completed_steps TEXT NOT NULL DEFAULT '[]',
                    session_start TEXT NOT NULL,
                    session_end TEXT NOT NULL,
                    plan_version_at_session_start INTEGER NOT NULL,
                    completion_percent REAL NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS plugin_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_plan_applications_ship
                    ON plan_applications(ship_instance_id);
                """
            )
            columns = {
                row["name"]
                for row in db.execute("PRAGMA table_info(active_session)").fetchall()
            }
            if "paused" not in columns:
                db.execute(
                    "ALTER TABLE active_session ADD COLUMN paused INTEGER NOT NULL DEFAULT 0"
                )

    def import_plan(
        self,
        ship_model: str,
        plan_name: str,
        source: dict[str, Any],
    ) -> str:
        """Insert or update a generic plan and return its stable plan id."""
        if not ship_model.strip() or not plan_name.strip():
            raise ValueError("ship_model and plan_name are required")

        source_json = json.dumps(source, sort_keys=True, separators=(",", ":"))
        source_hash = hashlib.sha256(source_json.encode("utf-8")).hexdigest()
        now = datetime.now(timezone.utc).isoformat()

        with self.get_db() as db:
            existing = db.execute(
                "SELECT id, plan_version, source_hash, source_json FROM plans "
                "WHERE ship_model = ? AND plan_name = ?",
                (ship_model, plan_name),
            ).fetchone()
            if existing is None:
                plan_id = hashlib.sha256(
                    f"{ship_model}\0{plan_name}".encode("utf-8")
                ).hexdigest()
                db.execute(
                    """
                    INSERT INTO plans
                        (id, ship_model, plan_name, source_json, source_hash,
                         import_date, last_modified)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (plan_id, ship_model, plan_name, source_json, source_hash, now, now),
                )
                self._publish_status()
                return plan_id

            if existing["source_hash"] == source_hash:
                return existing["id"]

            old_steps = self._plan_steps(existing["source_json"])
            new_steps = self._plan_steps(source_json)
            new_version = existing["plan_version"] + 1
            db.execute(
                """
                UPDATE plans
                SET source_json = ?, source_hash = ?, plan_version = ?,
                    last_modified = ?
                WHERE id = ?
                """,
                (source_json, source_hash, new_version, now, existing["id"]),
            )
            self._migrate_active_session(
                db, existing["id"], old_steps, new_steps, new_version, now
            )
            self._publish_status()
            return existing["id"]

    def diff_plan(
        self,
        plan_name: str,
        source: dict[str, Any],
    ) -> dict[str, Any]:
        """Compare an imported source with the currently stored plan."""
        if not plan_name.strip():
            raise ValueError("plan_name is required")
        incoming = self._plan_steps(json.dumps(source, sort_keys=True))
        ship_model = next(
            (
                value.strip()
                for value in (
                    source.get("ship_model"),
                    source.get("ship"),
                    source.get("Ship"),
                )
                if isinstance(value, str) and value.strip()
            ),
            None,
        )
        with self.get_db() as db:
            if ship_model:
                row = db.execute(
                    "SELECT source_json, plan_version FROM plans "
                    "WHERE plan_name = ? AND ship_model = ? "
                    "ORDER BY last_modified DESC LIMIT 1",
                    (plan_name.strip(), ship_model),
                ).fetchone()
            else:
                row = db.execute(
                    "SELECT source_json, plan_version FROM plans "
                    "WHERE plan_name = ? ORDER BY last_modified DESC LIMIT 1",
                    (plan_name.strip(),),
                ).fetchone()
        if row is None:
            return {
                "plan_name": plan_name,
                "current_version": None,
                "next_version": 1,
                **self._diff_steps([], incoming),
            }
        return {
            "plan_name": plan_name,
            "current_version": row["plan_version"],
            "next_version": row["plan_version"] + 1,
            **self._diff_steps(self._plan_steps(row["source_json"]), incoming),
        }

    def list_plans(self) -> list[dict[str, Any]]:
        with self.get_db() as db:
            rows = db.execute(
                "SELECT id, ship_model, plan_name, plan_version FROM plans "
                "ORDER BY ship_model, plan_name"
            ).fetchall()
        return [dict(row) for row in rows]

    def get_plan(self, plan_id: str) -> dict[str, Any]:
        """Return one complete plan, including normalized modules."""
        if not plan_id.strip():
            raise ValueError("plan_id is required")
        with self.get_db() as db:
            row = db.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
        if row is None:
            raise ValueError(f"Unknown plan: {plan_id}")
        plan = dict(row)
        plan["source"] = json.loads(plan.pop("source_json") or "{}")
        plan["modules"] = self._plan_steps(json.dumps(plan["source"]))
        return plan

    def update_plan(
        self,
        plan_id: str,
        ship_model: str,
        plan_name: str,
        source: dict[str, Any],
    ) -> str:
        """Update an existing plan without creating a different record."""
        current = self.get_plan(plan_id)
        if not ship_model.strip() or not plan_name.strip():
            raise ValueError("ship_model and plan_name are required")
        if (ship_model, plan_name) != (
            current["ship_model"],
            current["plan_name"],
        ):
            with self.get_db() as db:
                duplicate = db.execute(
                    "SELECT id FROM plans WHERE ship_model = ? AND plan_name = ?",
                    (ship_model, plan_name),
                ).fetchone()
            if duplicate is not None and duplicate["id"] != plan_id:
                raise ValueError("A plan with this ship model and name already exists")
        source_json = json.dumps(source, sort_keys=True, separators=(",", ":"))
        source_hash = hashlib.sha256(source_json.encode("utf-8")).hexdigest()
        now = datetime.now(timezone.utc).isoformat()
        old_steps = current["modules"]
        with self.get_db() as db:
            new_version = current["plan_version"] + (
                1 if current["source_hash"] != source_hash else 0
            )
            db.execute(
                """
                UPDATE plans
                SET ship_model = ?, plan_name = ?, source_json = ?,
                    source_hash = ?, plan_version = ?, last_modified = ?
                WHERE id = ?
                """,
                (
                    ship_model,
                    plan_name,
                    source_json,
                    source_hash,
                    new_version,
                    now,
                    plan_id,
                ),
            )
            if new_version != current["plan_version"]:
                self._migrate_active_session(
                    db,
                    plan_id,
                    old_steps,
                    self._plan_steps(source_json),
                    new_version,
                    now,
                )
        self._publish_status()
        return plan_id

    def delete_plan(self, plan_name: str) -> bool:
        """Delete a plan and its applications/session through foreign keys."""
        if not plan_name or not plan_name.strip():
            raise ValueError("plan_name is required")
        with self.get_db() as db:
            plan = db.execute(
                "SELECT id FROM plans WHERE plan_name = ?", (plan_name.strip(),)
            ).fetchone()
            if plan is None:
                raise ValueError(f"Unknown plan: {plan_name}")
            db.execute("DELETE FROM plans WHERE id = ?", (plan["id"],))
        if self._last_import and self._last_import.get("plan_id") == plan["id"]:
            self._clear_last_import()
            self._enter_import_state(IMPORT_STATE_IDLE)
        self._set_message_field(
            "plans", "plans_status", "plugin.sum.msg.deleted", {"plan": plan_name.strip()}
        )
        self._publish_status()
        return True

    def _loadout_baseline_for_ship(self, ship_instance_id: str) -> list[dict[str, Any]]:
        """Most recent Loadout modules for the given ship, from the journals,
        with the EngineerCraft results recorded after that Loadout applied."""
        try:
            log_files = self.journal_files()
        except (OSError, FileNotFoundError) as error:
            log("warn", f"Ship Upgrade Manager loadout baseline failed: {error}")
            return []
        # Newest first: crafts seen before reaching a Loadout happened after it.
        # A Loadout of another ship means those crafts belong to that ship.
        later_crafts: list[dict[str, Any]] = []
        for log_file in sorted(log_files, key=os.path.getmtime, reverse=True):
            try:
                with open(log_file, encoding="utf-8", errors="ignore") as handle:
                    lines = handle.readlines()
            except OSError as error:
                log("warn", f"Ship Upgrade Manager loadout baseline read failed: {error}")
                continue
            for line in reversed(lines):
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                event_name = entry.get("event")
                if event_name == "EngineerCraft":
                    later_crafts.append(entry)
                    continue
                if event_name != "Loadout":
                    continue
                if str(entry.get("ShipID", "")) != str(ship_instance_id):
                    later_crafts = []
                    continue
                modules = entry.get("Modules")
                if not isinstance(modules, list):
                    return []
                result = [module for module in modules if isinstance(module, dict)]
                for craft in reversed(later_crafts):
                    result = self._apply_engineer_craft(result, craft)
                return result
        return []

    @staticmethod
    def _normalize_module_id(module_id: str) -> str:
        normalized = module_id.strip()
        if normalized.startswith("$"):
            normalized = normalized[1:]
        if normalized.lower().endswith("_name;"):
            normalized = normalized[:-len("_name;")]
        return normalized.lower()

    @staticmethod
    def _blueprint_key(blueprint: str) -> str:
        """Comparable blueprint key: 'Engine_Dirty' and 'dirty' both map to 'dirty'."""
        last_segment = blueprint.strip().split("_")[-1]
        return re.sub(r"[^a-z0-9]", "", last_segment.lower())

    def _steps_satisfied_by_loadout(
        self, steps: list[dict[str, Any]], modules: list[dict[str, Any]]
    ) -> list[str]:
        """One installed module can satisfy at most one target step."""
        return [
            str(match["step"]["id"])
            for match in self._match_steps(steps, modules)
            if match["satisfied"]
        ]

    def start_session(
        self,
        plan_name: str,
        ship_instance_id: str,
        ship_custom_name: str = "",
    ) -> dict[str, Any]:
        if not plan_name.strip() or not ship_instance_id.strip():
            raise ValueError("plan_name and ship_instance_id are required")

        with self.get_db() as db:
            plan = db.execute(
                "SELECT * FROM plans WHERE plan_name = ? ORDER BY last_modified DESC LIMIT 1",
                (plan_name,),
            ).fetchone()
            if plan is None:
                raise ValueError(f"Unknown plan: {plan_name}")
            steps = self._plan_steps(plan["source_json"])
            now = datetime.now(timezone.utc).isoformat()
            session_id = hashlib.sha256(
                f"{plan['id']}\0{ship_instance_id}".encode()
            ).hexdigest()
            existing = db.execute(
                "SELECT id FROM active_session WHERE id = ?", (session_id,)
            ).fetchone()
            if existing:
                self._archive_active_session(
                    db, now, session_id=session_id
                )
            # Baseline: steps already satisfied by the ship's current loadout
            # (installed module, and matching engineering when the plan asks
            # for it) start completed instead of from zero.
            baseline_modules = self._loadout_baseline_for_ship(ship_instance_id)
            satisfied = self._steps_satisfied_by_loadout(steps, baseline_modules)
            current_step = next(
                (
                    index
                    for index, step in enumerate(steps)
                    if step["id"] not in satisfied
                ),
                len(steps),
            )
            db.execute(
                """
                INSERT INTO active_session
                    (id, plan_id, ship_instance_id, ship_custom_name,
                     completed_steps, current_step, session_start, last_activity,
                     plan_version_at_session_start, paused)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                ON CONFLICT(id) DO UPDATE SET
                    plan_id = excluded.plan_id,
                    ship_instance_id = excluded.ship_instance_id,
                    ship_custom_name = excluded.ship_custom_name,
                    completed_steps = excluded.completed_steps,
                    current_step = excluded.current_step,
                    session_start = excluded.session_start,
                    last_activity = excluded.last_activity,
                    plan_version_at_session_start = excluded.plan_version_at_session_start,
                    paused = 0
                """,
                (
                    session_id,
                    plan["id"],
                    ship_instance_id,
                    ship_custom_name or None,
                    json.dumps(satisfied),
                    current_step,
                    now,
                    now,
                    plan["plan_version"],
                ),
            )
            db.execute(
                """
                INSERT INTO plan_applications
                    (id, plan_id, ship_instance_id, ship_custom_name,
                     plan_version_applied)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(plan_id, ship_instance_id) DO UPDATE SET
                    ship_custom_name = excluded.ship_custom_name,
                    applied_date = CURRENT_TIMESTAMP,
                    plan_version_applied = excluded.plan_version_applied
                """,
                (
                    f"{plan['id']}:{ship_instance_id}",
                    plan["id"],
                    ship_instance_id,
                    ship_custom_name or None,
                    plan["plan_version"],
                ),
            )
        self._publish_status()
        return self.get_session(session_id) or {}

    def get_session(
        self, session_id: str | None = None, context: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        """Active session by id, else the plan in progress on the current ship.

        Sessions are keyed by the journal ShipID: without an explicit id the
        current ship's most recently active session is returned (None when
        that ship has no plan in progress), so swapping ships resumes the
        plan of the ship now flown. Only when no ship is identified yet does
        it fall back to the most recently active session.

        The AI does not know the internal session hash and passes whatever
        label it has (plan name, ship name/model, journal ShipID), so an
        unknown identifier is resolved against those labels (the current
        ship's sessions first), then falls back to the only active session."""
        session_id = session_id or (context or {}).get("session_id")
        identifier = str(
            session_id or (context or {}).get("ship_instance_id") or ""
        ).strip()
        with self.get_db() as db:
            query = """
                SELECT s.*, p.plan_name, p.ship_model, p.plan_version,
                       p.source_json
                FROM active_session s
                JOIN plans p ON p.id = s.plan_id
            """
            row = (
                db.execute(query + " WHERE s.id = ?", (identifier,)).fetchone()
                if identifier else None
            )
        if row is None:
            current_ship_id = self._resolve_current_ship_id()
            with self.get_db() as db:
                if identifier:
                    rows = db.execute(
                        query
                        + " ORDER BY s.ship_instance_id = ? DESC, s.last_activity DESC",
                        (current_ship_id,),
                    ).fetchall()
                    row = self._match_session_label(rows, identifier)
                    if row is None and len(rows) == 1:
                        row = rows[0]
                elif current_ship_id:
                    row = db.execute(
                        query
                        + " WHERE s.ship_instance_id = ? ORDER BY s.last_activity DESC LIMIT 1",
                        (current_ship_id,),
                    ).fetchone()
                else:
                    row = db.execute(
                        query + " ORDER BY s.last_activity DESC LIMIT 1"
                    ).fetchone()
        if row is None:
            return None
        session = dict(row)
        session["completed_steps"] = json.loads(session["completed_steps"] or "[]")
        session["steps"] = self._plan_steps(session.pop("source_json"))
        session["paused"] = bool(session["paused"])
        session["total_steps"] = len(session["steps"])
        return session

    def _match_session_label(
        self, rows: list[sqlite3.Row], identifier: str
    ) -> sqlite3.Row | None:
        """First session (most recent first) whose ship id, ship name, plan
        name or ship model matches the identifier: exact first, then partial."""
        needle = identifier.casefold()

        def labels(row: sqlite3.Row) -> list[str]:
            values = [
                row["ship_instance_id"],
                row["ship_custom_name"],
                row["plan_name"],
                row["ship_model"],
                self._ship_display_name(str(row["ship_model"] or "")),
            ]
            return [str(value).strip().casefold() for value in values if value]

        for row in rows:
            if needle in labels(row):
                return row
        for row in rows:
            if any(
                len(label) > 2 and (needle in label or label in needle)
                for label in labels(row)
            ):
                return row
        return None

    def list_sessions(self) -> list[dict[str, Any]]:
        """Return all active sessions, ordered by most recently active."""
        with self.get_db() as db:
            rows = db.execute(
                "SELECT id FROM active_session ORDER BY last_activity DESC"
            ).fetchall()
        sessions = []
        for row in rows:
            session = self.get_session(row["id"])
            if session:
                sessions.append(session)
        return sessions

    def complete_step(
        self, step_id: str, notes: str = "", session_id: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not step_id.strip():
            raise ValueError("step_id is required")
        session = self.get_session(session_id, context)
        if session is None:
            raise ValueError("No active upgrade session")
        if session["paused"]:
            raise ValueError("The active upgrade session is paused")
        valid_step_ids = {step["id"] for step in session["steps"]}
        if step_id not in valid_step_ids:
            raise ValueError(f"Unknown step: {step_id}")
        if step_id in session["completed_steps"]:
            return session

        completed = [*session["completed_steps"], step_id]
        current_step = next(
            (
                index
                for index, step in enumerate(session["steps"])
                if step["id"] not in completed
            ),
            len(session["steps"]),
        )
        now = datetime.now(timezone.utc).isoformat()
        with self.get_db() as db:
            db.execute(
                """
                UPDATE active_session
                SET current_step = ?, completed_steps = ?, last_activity = ?
                WHERE id = ?
                """,
                (current_step, json.dumps(completed), now, session["id"]),
            )
            db.execute(
                """
                INSERT INTO step_history
                    (id, plan_session_id, step_id, completed_at,
                     plan_version_at_completion, notes)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(plan_session_id, step_id) DO UPDATE SET
                    completed_at = excluded.completed_at,
                    plan_version_at_completion = excluded.plan_version_at_completion,
                    notes = excluded.notes
                """,
                (
                    f"{session['id']}:{step_id}",
                    session["id"],
                    step_id,
                    now,
                    session["plan_version"],
                    notes or None,
                ),
            )
        self._publish_status()
        return self.get_session(session["id"]) or {}

    def set_session_paused(
        self, paused: bool, session_id: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        session = self.get_session(session_id, context)
        if session is None:
            raise ValueError("No active upgrade session")
        with self.get_db() as db:
            db.execute(
                "UPDATE active_session SET paused = ?, last_activity = ? WHERE id = ?",
                (int(paused), datetime.now(timezone.utc).isoformat(), session["id"]),
            )
        self._publish_status()
        return self.get_session(session["id"]) or {}

    def stop_session(self, session_id: str | None = None,
                     context: dict[str, Any] | None = None) -> None:
        with self.get_db() as db:
            session = self.get_session(session_id, context)
            if session:
                self._archive_active_session(
                    db, datetime.now(timezone.utc).isoformat(), session_id=session["id"]
                )
                db.execute("DELETE FROM active_session WHERE id = ?", (session["id"],))
        self._publish_status()

    def list_session_history(self, limit: int = 50) -> list[dict[str, Any]]:
        if limit < 1:
            raise ValueError("limit must be positive")
        with self.get_db() as db:
            rows = db.execute(
                """
                SELECT h.*, p.plan_name, p.ship_model
                FROM session_history h
                JOIN plans p ON p.id = h.plan_id
                ORDER BY h.session_end DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        history = []
        for row in rows:
            item = dict(row)
            item["completed_steps"] = json.loads(item["completed_steps"] or "[]")
            history.append(item)
        return history

    def _archive_active_session(
        self, db: sqlite3.Connection, session_end: str, session_id: str | None = None
    ) -> None:
        where = "WHERE s.id = ?" if session_id else ""
        params = (session_id,) if session_id else ()
        row = db.execute(
            f"""
            SELECT s.*, p.plan_name, p.source_json
            FROM active_session s
            JOIN plans p ON p.id = s.plan_id
            {where}
            """, params
        ).fetchone()
        if row is None:
            return
        completed = json.loads(row["completed_steps"] or "[]")
        total = len(self._plan_steps(row["source_json"]))
        history_id = hashlib.sha256(
            f"{row['session_start']}\0{row['plan_id']}\0{row['ship_instance_id']}".encode()
        ).hexdigest()
        db.execute(
            """
            INSERT OR REPLACE INTO session_history
                (id, plan_id, ship_instance_id, ship_custom_name,
                 completed_steps, session_start, session_end,
                 plan_version_at_session_start, completion_percent)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                history_id,
                row["plan_id"],
                row["ship_instance_id"],
                row["ship_custom_name"],
                json.dumps(completed),
                row["session_start"],
                session_end,
                row["plan_version_at_session_start"],
                (len(completed) / total * 100) if total else 100,
            ),
        )

    @staticmethod
    def _plan_steps(source_json: str | None) -> list[dict[str, Any]]:
        if not source_json:
            return []
        source = json.loads(source_json)
        raw_steps = source.get("steps", []) if isinstance(source, dict) else []
        if not isinstance(raw_steps, list):
            raise ValueError("Plan steps must be a list")
        steps: list[dict[str, Any]] = []
        for index, raw_step in enumerate(raw_steps):
            if not isinstance(raw_step, dict):
                raise ValueError(f"Plan step {index} must be an object")
            step = dict(raw_step)
            step_id = step.get("id", f"step_{index}")
            if not isinstance(step_id, str) or not step_id.strip():
                raise ValueError(f"Plan step {index} has an invalid id")
            step["id"] = step_id
            steps.append(step)
        if len({step["id"] for step in steps}) != len(steps):
            raise ValueError("Plan step ids must be unique")
        return steps

    @staticmethod
    def _step_signature(step: dict[str, Any]) -> str:
        comparable = {key: value for key, value in step.items() if key != "id"}
        return json.dumps(comparable, sort_keys=True, separators=(",", ":"))

    @classmethod
    def _diff_steps(
        cls,
        old_steps: list[dict[str, Any]],
        new_steps: list[dict[str, Any]],
    ) -> dict[str, Any]:
        old_by_id = {step["id"]: step for step in old_steps}
        new_by_id = {step["id"]: step for step in new_steps}
        added = [new_by_id[key] for key in new_by_id.keys() - old_by_id.keys()]
        removed = [old_by_id[key] for key in old_by_id.keys() - new_by_id.keys()]
        changed = [
            {"before": old_by_id[key], "after": new_by_id[key]}
            for key in old_by_id.keys() & new_by_id.keys()
            if cls._step_signature(old_by_id[key]) != cls._step_signature(new_by_id[key])
        ]
        changed_ids = {item["after"]["id"] for item in changed}
        unchanged = [
            new_by_id[key]
            for key in old_by_id.keys() & new_by_id.keys()
            if key not in changed_ids
        ]
        return {
            "added": added,
            "removed": removed,
            "changed": changed,
            "unchanged": unchanged,
            "added_count": len(added),
            "removed_count": len(removed),
            "changed_count": len(changed),
        }

    def _migrate_active_session(
        self,
        db: sqlite3.Connection,
        plan_id: str,
        old_steps: list[dict[str, Any]],
        new_steps: list[dict[str, Any]],
        new_version: int,
        now: str,
    ) -> None:
        sessions = db.execute(
            "SELECT id, completed_steps FROM active_session WHERE plan_id = ?",
            (plan_id,),
        ).fetchall()
        old_by_id = {step["id"]: step for step in old_steps}
        new_by_id = {step["id"]: step for step in new_steps}
        for session in sessions:
            completed = json.loads(session["completed_steps"] or "[]")
            migrated = [
                step_id for step_id in completed
                if step_id in new_by_id and step_id in old_by_id
                and self._step_signature(old_by_id[step_id])
                == self._step_signature(new_by_id[step_id])
            ]
            current_step = next(
                (index for index, step in enumerate(new_steps)
                 if step["id"] not in migrated), len(new_steps)
            )
            db.execute(
                """UPDATE active_session SET completed_steps = ?, current_step = ?,
                   plan_version_at_session_start = ?, last_activity = ? WHERE id = ?""",
                (json.dumps(migrated), current_step, new_version, now, session["id"]),
            )
            if migrated:
                placeholders = ",".join("?" for _ in migrated)
                db.execute(
                    f"DELETE FROM step_history WHERE plan_session_id = ? AND step_id NOT IN ({placeholders})",
                    (session["id"], *migrated),
                )
            else:
                db.execute("DELETE FROM step_history WHERE plan_session_id = ?", (session["id"],))

    def _publish_status(self) -> None:
        self._set_plan_rows(self._make_plan_rows())

        session = self.get_session()
        if session is None:
            self._set_message_field("session", "session_summary", "plugin.sum.noSession")
        else:
            total = session["total_steps"]
            completed = len(session["completed_steps"])
            percent = round((completed / total) * 100) if total else 0
            ship = session["ship_custom_name"] or session["ship_instance_id"]
            self._set_message_field(
                "session",
                "session_summary",
                "plugin.sum.msg.sessionActive",
                {
                    "plan": session["plan_name"],
                    "ship": ship,
                    "step": session["current_step"],
                    "total": total,
                    "pct": percent,
                },
            )
        self._list_field("session", "session_modules")["items"] = self._session_module_rows()
        self._republish()

    def _republish(self) -> None:
        """Push mutated field rows/paragraphs to the UI.

        Broadcast-only (no config write), so it is safe from the journal
        watcher thread. The manager backref is available in config state;
        without it (no registration) there is nothing to update."""
        manager = self._plugin_manager()
        if manager is None:
            return
        manager.republish_settings()

    def _plugin_manager(self) -> "PluginManager | None":
        """Manager backref (config state), else the runtime helper's one."""
        if self._manager is not None:
            return self._manager
        return getattr(self.helper, "_plugin_manager", None)

    def journal_files(self) -> list[str]:
        """Journal files of the folder set in the COVAS:NEXT options
        (ed_journal_path), read live so a path change applies at once."""
        return _journal_files(getattr(self._plugin_manager(), "config", None))

    def _plan_module_count(self, plan_id: str) -> int:
        with self.get_db() as db:
            row = db.execute(
                "SELECT source_json FROM plans WHERE id = ?", (plan_id,)
            ).fetchone()
        if row is None:
            return 0
        return len(self._plan_steps(row["source_json"]))
