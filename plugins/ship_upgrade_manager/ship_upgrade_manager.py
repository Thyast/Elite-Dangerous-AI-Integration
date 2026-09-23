import hashlib
from html import escape
import json
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from pydantic import BaseModel, Field

from lib.Event import Event, GameEvent, ProjectedEvent
from lib.EventManager import Projection
from lib.Logger import log
from lib.Config import get_asset_path, get_ed_journals_path
from lib.PluginBase import PluginBase, PluginManifest
from lib.PluginHelper import PluginHelper
from lib.PluginSettingDefinitions import (
    ButtonSetting,
    ErrorSetting,
    ListAction,
    ListRow,
    ListSetting,
    ParagraphSetting,
    PluginSettings,
    SettingsGrid,
    SettingBase,
    TextAreaSetting,
    TextSetting,
)
from .parsers import PlanParseError, parse_plan_input


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


# Class (1-5) to E:D letter grade: 1=E, 2=D, 3=C, 4=B, 5=A.
CLASS_LETTERS = {1: "E", 2: "D", 3: "C", 4: "B", 5: "A"}

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


class _JournalWatcher(threading.Thread):
    """Tails the Elite journals while the runtime is not running.

    Journal side effects only reach the plugin after the user presses Run;
    this watcher lets the plugin track ship changes from C:N startup in
    config state. While the runtime runs, the EventManager delivers the
    same events and the watcher keeps tailing but drops the entries, so
    there is never double processing."""

    def __init__(self, plugin: "ShipUpgradeManagerPlugin"):
        super().__init__(daemon=True, name="ship-upgrade-journal-watcher")
        self.plugin = plugin
        self._path = ""
        self._offset = 0
        self._remainder = ""

    def run(self) -> None:
        while True:
            try:
                self._poll()
            except Exception as error:
                log("warning", f"Ship Upgrade Manager journal watcher error: {error}")
            time.sleep(1.0)

    def _poll(self) -> None:
        try:
            journals_path = get_ed_journals_path({})
            log_files = [
                os.path.join(journals_path, name)
                for name in os.listdir(journals_path)
                if os.path.isfile(os.path.join(journals_path, name))
                and name.startswith("Journal.")
            ]
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
        if not isinstance(entry, dict) or not self.plugin._watch_active:
            return
        self.plugin._handle_ship_event(entry)


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
    StateModel = ShipUpgradeState

    def process(self, event: Event) -> None:
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


class PlanChangeParams(BaseModel):
    plan_input: str = Field(description="JSON loadout or supported plan URL")


class ShipUpgradeManagerPlugin(PluginBase):
    """Persist generic ship plans and per-ship upgrade sessions."""

    settings_schema_version = 1

    def __init__(self, plugin_manifest: PluginManifest):
        super().__init__(plugin_manifest)
        self.helper: PluginHelper | None = None
        self._current_ship_id = ""
        self._current_ship_name = ""
        self._current_loadout_modules: list[dict[str, Any]] = []
        self._last_plan_filter = ""
        self._watch_active = True
        self._initialize_database()
        self._last_import = self._load_last_import()
        self._import_state = IMPORT_STATE_IMPORTED if self._last_import else IMPORT_STATE_IDLE
        self.settings_config = self._build_settings_config()
        _JournalWatcher(self).start()

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

    def _sync_grids(self, error: tuple[str, dict[str, str | int | float]] | None = None) -> None:
        self.settings_config["grids"] = self._build_grids(error)
        for grid in self.settings_config["grids"]:
            for field in grid["fields"]:
                if field["type"] in {"paragraph", "error"}:
                    self.settings[field["key"]] = field.get("content", "")

    def _base_field(self, key: str, type_: str, label: str | None = None) -> SettingBase:
        return {
            "key": key,
            "label": label,
            "type": type_,  # type: ignore[typeddict-item]
            "readonly": type_ in {"paragraph", "error", "list"},
            "placeholder": None,
        }

    def _button(self, key: str, label: str, icon: str | None = None) -> ButtonSetting:
        field: ButtonSetting = self._base_field(key, "button", label)  # type: ignore[assignment]
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
        field: ParagraphSetting = self._base_field(key, "paragraph", label)  # type: ignore[assignment]
        field["content"] = content
        if params:
            field["params"] = params
        return field

    def _import_fields(
        self,
        error: tuple[str, dict[str, str | int | float]] | None = None,
    ) -> list[SettingBase]:
        state = self._import_state
        if state == IMPORT_STATE_IDLE:
            # The import entry point lives in the plans grid header action;
            # the tunnel grid itself only exists while a flow is open.
            return []
        if state == IMPORT_STATE_DATA:
            textarea: TextAreaSetting = self._base_field("plan_input", "textarea", "plugin.sum.planDataLabel")  # type: ignore[assignment]
            textarea.update({
                "placeholder": "plugin.sum.planDataPlaceholder",
                "default_value": "",
                "rows": 8,
                "cols": 60,
            })
            fields: list[SettingBase] = [
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

    def _plans_fields(self) -> list[SettingBase]:
        plan_filter: TextSetting = self._base_field("plan_filter", "filter", "plugin.sum.search")  # type: ignore[assignment]
        plan_filter.update({
            "placeholder": "plugin.sum.searchPlaceholder",
            "default_value": "",
            "max_length": 100,
            "min_length": 0,
            "hidden": False,
        })
        plans_list: ListSetting = self._base_field("available_plans", "list", None)  # type: ignore[assignment]
        plans_list.update({
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
        })
        delete_status = self._paragraph("plans_status", str(self.settings.get("plans_status", "") or ""))
        return [plan_filter, plans_list, delete_status]

    def _session_fields(self) -> list[SettingBase]:
        session_modules: ListSetting = self._base_field("session_modules", "list", None)  # type: ignore[assignment]
        session_modules.update({
            "placeholder": "plugin.sum.noSession",
            "items": self._session_module_rows(),
            "row_actions": [],
            "unit": "module",
        })
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

    def _module_category(self, item: str) -> str:
        """Outfitting-style category of a module, from its FD id."""
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
        """Per-step detail of the most recently active session: target module
        vs the module currently carried on the ship, grouped by outfitting
        category."""
        with self.get_db() as db:
            session = db.execute(
                """
                SELECT s.id, s.completed_steps, s.ship_instance_id, p.source_json
                FROM active_session s JOIN plans p ON p.id = s.plan_id
                ORDER BY s.last_activity DESC LIMIT 1
                """
            ).fetchone()
        if session is None:
            return []
        steps = self._plan_steps(session["source_json"])
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
            target_params: dict[str, str | int | float] = {
                "blueprint": _blueprint_family_display(required_blueprint),
            }
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
                has_engineering = bool(match["eng_blueprint"]) and bool(current_level)
                blueprint_ok = match["satisfied"] or (
                    has_engineering
                    and match["eng_blueprint"].lower().replace(" ", "")
                    == re.sub(r"[^a-z0-9]", "", _blueprint_family_display(required_blueprint).lower())
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
                "group": self._module_category(item),
                "meta": meta,
                "params": params or {},
                "pending": not match["satisfied"],
            }
            rows.append(row)
        category_index = {key: index for index, key in enumerate(CATEGORY_ORDER)}
        rows.sort(key=lambda row: (category_index.get(str(row.get("group")), 99), row["title"]))
        return rows

    def _grid(self, key: str) -> SettingsGrid:
        return next(grid for grid in self.settings_config["grids"] if grid["key"] == key)

    def _field(self, grid_key: str, field_key: str) -> SettingBase:
        return next(
            field for field in self._grid(grid_key)["fields"] if field["key"] == field_key
        )

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

        Returns (english fallback name, i18n key, size/class grade) where the
        grade uses the E:D letter notation (1=E … 5=A, size 0 → letter only).
        The i18n key ('module.<family>') is resolved by the UI against the
        module dictionary; the english name is the fallback."""
        family, size, module_class = self._split_module_id(item)
        family_key = re.sub(r"[^a-z0-9]", "", family)
        key = f"module.{_spec_module_key(family_key)}"
        pretty = next(
            (name for name_key, name in MODULE_FAMILIES if family_key.startswith(name_key)),
            None,
        )
        if pretty is None:
            pretty = family.replace("_", " ").strip().title()
        grade = None
        if module_class:
            letter = CLASS_LETTERS.get(int(module_class))
            grade = f"{size}{letter}" if size and size != "0" else (letter or "?")
        return pretty, key, grade

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
        the currently reached engineering level with its blueprint family, and
        whether the step requirement is satisfied (strict blueprint rule)."""
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
                    and candidate["item"] == item
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
                        if not used[index] and candidate["item"] == item
                    ),
                    None,
                )
            if candidate_index is None:
                results.append({"step": step, "installed": False, "slot": "", "eng_current": None, "eng_blueprint": "", "satisfied": False})
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
                satisfied = True
                eng_current = None
            else:
                blueprint_ok = required_blueprint and self._blueprint_key(
                    candidate["blueprint"]
                ) == self._blueprint_key(required_blueprint)
                satisfied = bool(
                    blueprint_ok
                    and current_level is not None
                    and current_level >= int(float(required_level))
                )
                eng_current = current_level if blueprint_ok else 0
            blueprint_family = ""
            if candidate["blueprint"]:
                blueprint_family = re.sub(
                    r"(?<!^)(?=[A-Z])", " ", candidate["blueprint"].split("_")[-1]
                )
            results.append(
                {
                    "step": step,
                    "installed": True,
                    "slot": candidate["slot"],
                    "eng_current": eng_current,
                    "eng_blueprint": blueprint_family,
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
        of target levels vs the sum of levels currently reached — a level only
        counts when the blueprint matches the target (strict)."""
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
            if match["eng_current"] is not None:
                eng_current += match["eng_current"]
        total = len(steps)
        return {
            "modules_done": modules_done,
            "modules_total": total,
            "modules_pct": round((modules_done / total) * 100) if total else 0,
            "eng_current": eng_current,
            "eng_target": eng_target,
            "eng_pct": round((eng_current / eng_target) * 100) if eng_target else 0,
        }

    def _plan_progress(self, plan_id: str, ship_model: str) -> list[dict[str, Any]]:
        with self.get_db() as db:
            plan_row = db.execute(
                "SELECT source_json FROM plans WHERE id = ?", (plan_id,)
            ).fetchone()
            sessions = db.execute(
                """
                SELECT ship_custom_name, ship_instance_id, completed_steps, paused, last_activity
                FROM active_session WHERE plan_id = ? ORDER BY last_activity DESC
                """,
                (plan_id,),
            ).fetchall()
        if plan_row is None or not sessions:
            return []
        steps = self._plan_steps(plan_row["source_json"])
        display_model = self._ship_display_name(ship_model)
        progress: list[dict[str, Any]] = []
        for session in sessions:
            completed = json.loads(session["completed_steps"] or "[]")
            completed_set = set(completed)
            next_step = next((step for step in steps if step["id"] not in completed_set), None)
            custom_name = session["ship_custom_name"] or ""
            entry: dict[str, Any] = {
                # No custom name yet: show the public ship model instead of the
                # raw journal ship id.
                "ship": custom_name or display_model,
                "ship_model": display_model if custom_name else "",
                "paused": bool(session["paused"]),
            }
            entry.update(self._progress_metrics(steps, self._loadout_for_ship(session["ship_instance_id"])))
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
                blueprint = str(engineering.get("BlueprintName") or engineering.get("blueprint") or "").split("_")[-1]
                if blueprint and blueprint.lower() != "none":
                    entry["next_engineering"] = re.sub(r"(?<!^)(?=[A-Z])", " ", blueprint)
            progress.append(entry)
        return progress

    def _set_plan_rows(self, rows: list[ListRow]) -> None:
        field: ListSetting = self._field("plans", "available_plans")  # type: ignore[assignment]
        field["items"] = rows

    def _set_message_field(self, grid_key: str, field_key: str, content: str, params: dict[str, str | int | float] | None = None) -> None:
        field = self._field(grid_key, field_key)
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

    def on_chat_start(self, helper: PluginHelper) -> None:
        self.helper = helper
        self._watch_active = False
        self._initialize_database()
        self._reload_state_from_database()
        helper.register_projection(ShipUpgradeProjection())
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
            description="Describe the next module of the active ship upgrade session",
            parameters=SessionActionParams,
            method=lambda args, context: self._next_step_action(args.session_id, context),
            action_type="ship",
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
        helper.register_sideeffect(self._on_event)
        helper.register_status_generator(self._status_generator)
        self._publish_status()

    def on_chat_stop(self, helper: PluginHelper) -> None:
        self.helper = None
        self._watch_active = True

    def on_settings_changed(self) -> None:
        plan_filter = self.settings.get("plan_filter", "")
        if plan_filter != getattr(self, "_last_plan_filter", ""):
            self._publish_status()

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
            log("warning", f"Unknown Ship Upgrade Manager settings button: {key}")

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

    def _session_pause_action(self, session_id=None, context=None) -> str:
        self.set_session_paused(True, session_id, context)
        return "Ship upgrade session paused."

    def _session_resume_action(self, session_id=None, context=None) -> str:
        self.set_session_paused(False, session_id, context)
        return "Ship upgrade session resumed."

    def _session_stop_action(self, session_id=None, context=None) -> str:
        self.stop_session(session_id, context)
        return "Ship upgrade session stopped."

    def _next_step_action(self, session_id=None, context=None) -> str:
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
        return (
            f"Next module, {len(session['completed_steps']) + 1} of "
            f"{session['total_steps']}: {step.get('label', step['id'])}."
        )

    def _list_plans_action(self) -> str:
        plans = self.list_plans()
        if not plans:
            return "No ship upgrade plans are imported."
        return "Available plans: " + "; ".join(
            f"{plan['plan_name']} for {plan['ship_model']} version {plan['plan_version']}"
            for plan in plans
        )

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
            journals_path = get_ed_journals_path({})
            log_files = [
                os.path.join(journals_path, name)
                for name in os.listdir(journals_path)
                if os.path.isfile(os.path.join(journals_path, name))
                and name.startswith("Journal.")
            ]
        except (OSError, FileNotFoundError) as error:
            log("warning", f"Ship Upgrade Manager journal detection failed: {error}")
            return {}
        for log_file in sorted(log_files, key=os.path.getmtime, reverse=True):
            try:
                with open(log_file, encoding="utf-8", errors="ignore") as handle:
                    lines = handle.readlines()
            except OSError as error:
                log("warning", f"Ship Upgrade Manager journal read failed: {error}")
                continue
            for line in reversed(lines):
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ship_id = entry.get("ShipID")
                if ship_id is None:
                    continue
                detected: dict[str, str] = {"id": str(ship_id)}
                ship_name = entry.get("ShipName")
                if isinstance(ship_name, str) and ship_name.strip():
                    detected["name"] = ship_name.strip()
                return detected
        return {}

    def _rename_plan_from_settings(self, plan_id: str, new_name: str) -> None:
        try:
            self.rename_plan(plan_id, new_name)
        except ValueError as error:
            log("error", f"Ship Upgrade Manager plan rename failed: {error}")
            self._set_message_field(
                "plans", "plans_status", "plugin.sum.errRename", {"detail": str(error)}
            )

    def _start_plan_session_from_settings(self, plan_id: str) -> None:
        if not self._current_ship_id:
            detected = self._detect_ship_from_journal()
            self._current_ship_id = detected.get("id", "")
            if detected.get("name") and not self._current_ship_name:
                self._current_ship_name = detected["name"]
        if not self._current_ship_id:
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
        if not isinstance(new_name, str) or not new_name.strip():
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

    def _on_event(self, event: Event, _context: dict[str, Any]) -> None:
        if not isinstance(event, GameEvent):
            return
        self._handle_ship_event(event.content)

    def _handle_ship_event(self, content: dict[str, Any]) -> None:
        event_name = content.get("event")
        if event_name not in {
            "Loadout",
            "ModuleInfo",
            "ModuleBuy",
            "ModuleSwap",
            "ModuleStore",
            "ModuleRetrieve",
        }:
            return
        ship_id = str(
            content.get("ShipID")
            or content.get("ShipIdent")
            or content.get("ShipName")
            or content.get("Ship")
            or ""
        )
        if ship_id:
            self._current_ship_id = ship_id
            ship_name = content.get("ShipName")
            if isinstance(ship_name, str) and ship_name.strip():
                self._current_ship_name = ship_name.strip()
        if event_name in {"Loadout", "ModuleInfo"} and (
            not ship_id or not self._current_ship_id or ship_id == self._current_ship_id
        ):
            loadout_modules = content.get("Modules")
            if isinstance(loadout_modules, list):
                self._current_loadout_modules = [
                    module for module in loadout_modules if isinstance(module, dict)
                ]
        modules = self._event_modules(content)
        with self.get_db() as db:
            rows = db.execute("SELECT id, ship_instance_id, ship_custom_name FROM active_session").fetchall()
        if not ship_id and len({str(row["ship_instance_id"]) for row in rows}) > 1:
            log(
                "warning",
                "Skipping ship upgrade detection because the event has no ship identifier "
                "and multiple ships have active sessions",
            )
            return
        for row in rows:
            session = self.get_session(row["id"])
            if session is None or session["paused"]:
                continue
            if ship_id and ship_id not in {
                str(session["ship_instance_id"]),
                str(session.get("ship_custom_name") or ""),
            }:
                continue
            for step in session["steps"]:
                if step["id"] in session["completed_steps"]:
                    continue
                if any(self._module_matches_step(step, module) for module in modules):
                    try:
                        self.complete_step(
                            step["id"], "Detected from Loadout event", session["id"]
                        )
                    except ValueError as error:
                        log("warning", f"Could not auto-complete ship upgrade step: {error}")
        # The progress bars reflect the ship's current loadout, so refresh
        # whenever a relevant journal event changes that state.
        self._publish_status()

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
        return []

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
        if not isinstance(item, str) or not item:
            return False
        if module.get("item") != item or (slot and module.get("slot") != slot):
            return False
        expected_engineering = step.get("engineering")
        if not expected_engineering:
            return True
        actual_engineering = module.get("engineering")
        if not isinstance(actual_engineering, dict):
            return False
        for key in ("BlueprintName", "Level", "ExperimentalEffect"):
            expected = expected_engineering.get(key)
            if expected is not None and actual_engineering.get(key) != expected:
                return False
        return True

    def _status_generator(self, _states: dict[str, BaseModel]) -> list[tuple[str, Any]]:
        session = self.get_session()
        if session is None:
            return []
        remaining = [
            step for step in session["steps"]
            if step["id"] not in session["completed_steps"]
        ]
        next_step = remaining[0].get("label", remaining[0]["id"]) if remaining else "complete"
        return [
            (
                "Ship upgrade",
                f"{session['plan_name']} on "
                f"{session['ship_custom_name'] or session['ship_instance_id']}: "
                f"{len(session['completed_steps'])}/{session['total_steps']} complete; "
                f"next: {next_step}",
            )
        ]

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
    def get_db(self) -> Iterator[sqlite3.Connection]:
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
        if not isinstance(plan_name, str) or not plan_name.strip():
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
        """Most recent Loadout modules for the given ship, from the journals."""
        try:
            journals_path = get_ed_journals_path({})
            log_files = [
                os.path.join(journals_path, name)
                for name in os.listdir(journals_path)
                if os.path.isfile(os.path.join(journals_path, name))
                and name.startswith("Journal.")
            ]
        except (OSError, FileNotFoundError) as error:
            log("warning", f"Ship Upgrade Manager loadout baseline failed: {error}")
            return []
        for log_file in sorted(log_files, key=os.path.getmtime, reverse=True):
            try:
                with open(log_file, encoding="utf-8", errors="ignore") as handle:
                    lines = handle.readlines()
            except OSError as error:
                log("warning", f"Ship Upgrade Manager loadout baseline read failed: {error}")
                continue
            for line in reversed(lines):
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("event") != "Loadout":
                    continue
                if str(entry.get("ShipID", "")) != str(ship_instance_id):
                    continue
                modules = entry.get("Modules")
                return modules if isinstance(modules, list) else []
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
        """Step ids already satisfied by the ship's current loadout.

        A step is satisfied when a loadout module carries the same item and,
        when the plan requires engineering, a blueprint of the same family at
        an equal or higher level. Steps without an installed module stay open,
        so engineering-only progress starts from the real ship state."""
        loadout: dict[str, list[tuple[str, Any]]] = {}
        for module in modules:
            item = self._normalize_module_id(str(module.get("Item") or ""))
            if not item or item == "null":
                continue
            engineering = module.get("Engineering") or {}
            blueprint = str(engineering.get("BlueprintName") or engineering.get("blueprint") or "")
            level = engineering.get("Level") or engineering.get("level")
            loadout.setdefault(item, []).append((blueprint, level))
        satisfied: list[str] = []
        for step in steps:
            item = self._normalize_module_id(
                str(step.get("item") or step.get("id") or "")
            )
            candidates = loadout.get(item)
            if not candidates:
                continue
            required = step.get("engineering") or {}
            required_blueprint = str(
                required.get("BlueprintName") or required.get("blueprint") or ""
            )
            required_level = required.get("Level") or required.get("level")
            if required_blueprint or required_level:
                satisfied_module = any(
                    (
                        not required_blueprint
                        or self._blueprint_key(blueprint)
                        == self._blueprint_key(required_blueprint)
                    )
                    and (
                        required_level is None
                        or (
                            isinstance(level, (int, float))
                            and level >= required_level
                        )
                    )
                    for blueprint, level in candidates
                    if blueprint
                )
                if not satisfied_module:
                    continue
            satisfied.append(str(step["id"]))
        return satisfied

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
        session_id = session_id or (context or {}).get("session_id")
        with self.get_db() as db:
            query = """
                SELECT s.*, p.plan_name, p.ship_model, p.plan_version,
                       p.source_json
                FROM active_session s
                JOIN plans p ON p.id = s.plan_id
            """
            if session_id:
                query += " WHERE s.id = ?"
                params: tuple[Any, ...] = (session_id,)
            elif context and context.get("ship_instance_id"):
                query += " WHERE s.ship_instance_id = ? ORDER BY s.last_activity DESC LIMIT 1"
                params = (str(context["ship_instance_id"]),)
            else:
                query += " ORDER BY s.last_activity DESC LIMIT 1"
                params = ()
            row = db.execute(query, params).fetchone()
        if row is None:
            return None
        session = dict(row)
        session["completed_steps"] = json.loads(session["completed_steps"] or "[]")
        session["steps"] = self._plan_steps(session.pop("source_json"))
        session["paused"] = bool(session["paused"])
        session["total_steps"] = len(session["steps"])
        return session

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

        with self.get_db() as db:
            session = db.execute(
                """
                SELECT p.plan_name, s.ship_custom_name, s.ship_instance_id,
                       s.current_step, s.completed_steps, s.plan_id
                FROM active_session s
                JOIN plans p ON p.id = s.plan_id
                ORDER BY s.last_activity DESC
                LIMIT 1
                """
            ).fetchone()

        if session is None:
            self._set_message_field("session", "session_summary", "plugin.sum.noSession")
        else:
            total = self._plan_module_count(session["plan_id"])
            completed = len(json.loads(session["completed_steps"] or "[]"))
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
        module_rows_field: ListSetting = self._field("session", "session_modules")  # type: ignore[assignment]
        module_rows_field["items"] = self._session_module_rows()
        self._republish()

    def _republish(self) -> None:
        """Push mutated field rows/paragraphs to the UI.

        Broadcast-only (no config write), so it is safe from the journal
        watcher thread. The manager backref is available in config state;
        without it (no registration) there is nothing to update."""
        manager = self._manager
        if manager is None and self.helper is not None:
            manager = self.helper._plugin_manager
        if manager is None:
            return
        manager.republish_settings()

    def _plan_module_count(self, plan_id: str) -> int:
        with self.get_db() as db:
            row = db.execute(
                "SELECT source_json FROM plans WHERE id = ?", (plan_id,)
            ).fetchone()
        if row is None:
            return 0
        return len(self._plan_steps(row["source_json"]))
