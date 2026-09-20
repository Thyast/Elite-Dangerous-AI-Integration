import hashlib
from html import escape
import json
import os
import re
import sqlite3
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
IMPORT_STATE_DIFF = "diff"
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
        self._pending_diff: dict[str, Any] | None = None
        self._last_plan_filter = ""
        self._initialize_database()
        self._last_import = self._load_last_import()
        self._import_state = IMPORT_STATE_IMPORTED if self._last_import else IMPORT_STATE_IDLE
        self.settings_config = self._build_settings_config()

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
                self._button("analyze_plan", "plugin.sum.btn.analyze"),
                self._button("cancel_import", "common.cancel"),
            ]
        elif state == IMPORT_STATE_DIFF:
            fields = [
                self._paragraph(
                    "diff_preview",
                    str(self.settings.get("diff_preview", "")),
                    label="plugin.sum.planChanges",
                ),
                self._button("confirm_import", "plugin.sum.btn.confirm"),
                self._button("modify_data", "plugin.sum.btn.modify"),
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
                self._paragraph(
                    "last_changes",
                    str(record.get("diff_html", "")),
                    label="plugin.sum.lastChanges",
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
        return [
            self._paragraph(
                "session_summary",
                str(self.settings.get("session_summary", "") or "plugin.sum.noSession"),
                label="plugin.sum.session",
            )
        ]

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
        total = len(steps)
        progress: list[dict[str, Any]] = []
        for session in sessions:
            completed = json.loads(session["completed_steps"] or "[]")
            completed_set = set(completed)
            next_step = next((step for step in steps if step["id"] not in completed_set), None)
            entry: dict[str, Any] = {
                "ship": session["ship_custom_name"] or session["ship_instance_id"],
                "ship_model": ship_model,
                "paused": bool(session["paused"]),
                "completed": len(completed),
                "total": total,
                "pct": round((len(completed) / total) * 100) if total else 0,
            }
            if next_step is not None:
                entry["next_label"] = str(
                    next_step.get("label") or next_step.get("item") or next_step.get("id", "")
                )
                engineering = next_step.get("engineering") or {}
                grade = engineering.get("Level")
                if grade is not None:
                    entry["next_grade"] = grade
                blueprint = str(engineering.get("BlueprintName", "")).split("_")[-1]
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

    def on_settings_changed(self) -> None:
        plan_filter = self.settings.get("plan_filter", "")
        if plan_filter != getattr(self, "_last_plan_filter", ""):
            self._publish_status()

    def on_settings_button(self, key: str, value: str | None = None) -> None:
        if key == "import_plan":
            self._pending_diff = None
            self._enter_import_state(IMPORT_STATE_DATA)
        elif key == "analyze_plan":
            self._analyze_plan_input()
        elif key == "confirm_import":
            self._confirm_plan_import()
        elif key == "modify_data":
            self._enter_import_state(IMPORT_STATE_DATA)
        elif key == "cancel_import":
            self.settings["plan_input"] = ""
            self._pending_diff = None
            self._enter_import_state(IMPORT_STATE_IDLE)
        elif key == "new_import":
            self.settings["plan_input"] = ""
            self._pending_diff = None
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

    def _analyze_plan_input(self) -> None:
        value = self.settings.get("plan_input", "")
        try:
            normalized = parse_plan_input(value)
            diff = self.diff_plan(normalized["plan_name"], normalized)
        except (PlanParseError, ValueError, TypeError, json.JSONDecodeError) as error:
            log("error", f"Ship Upgrade Manager plan analysis failed: {error}")
            self._enter_import_state(
                IMPORT_STATE_DATA, error=("plugin.sum.errParse", {"detail": str(error)})
            )
            return
        self._pending_diff = {
            "normalized": normalized,
            "diff": diff,
            "rendered": self._format_plan_diff(diff),
        }
        self.settings["diff_preview"] = self._pending_diff["rendered"]
        self._enter_import_state(IMPORT_STATE_DIFF)

    def _confirm_plan_import(self) -> None:
        pending = self._pending_diff
        if pending is None:
            self._enter_import_state(
                IMPORT_STATE_DATA,
                error=("plugin.sum.errParse", {"detail": "no analyzed plan"}),
            )
            return
        normalized = pending["normalized"]
        try:
            plan_id = self.import_plan(
                normalized["ship_model"], normalized["plan_name"], normalized
            )
        except (ValueError, TypeError) as error:
            log("error", f"Ship Upgrade Manager plan confirmation failed: {error}")
            self._enter_import_state(
                IMPORT_STATE_DIFF, error=("plugin.sum.errApply", {"detail": str(error)})
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
            "diff_html": pending["rendered"],
        }
        self._save_last_import(record)
        self._last_import = record
        self._publish_status()
        self._enter_import_state(IMPORT_STATE_IMPORTED)
        log("info", f"Confirmed Ship Upgrade Manager plan {plan_id} version {record['version']}")

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

    def _detect_ship_from_journal(self) -> str:
        """Best-effort read of the journals to find the current ship id.

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
            return ""
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
                if ship_id is not None:
                    return str(ship_id)
        return ""

    def _rename_plan_from_settings(self, plan_id: str, new_name: str) -> None:
        try:
            self.rename_plan(plan_id, new_name)
        except ValueError as error:
            log("error", f"Ship Upgrade Manager plan rename failed: {error}")
            self._set_message_field(
                "plans", "plans_status", "plugin.sum.errRename", {"detail": str(error)}
            )

    def _start_plan_session_from_settings(self, plan_id: str) -> None:
        ship_id = self._current_ship_id or self._detect_ship_from_journal()
        if ship_id:
            self._current_ship_id = ship_id
        if not ship_id:
            self._set_message_field("plans", "plans_status", "plugin.sum.errNoShip")
            return
        try:
            plan = self.get_plan(plan_id)
            self.start_session(plan["plan_name"], ship_id)
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

    @staticmethod
    def _format_plan_diff(diff: dict[str, Any]) -> str:
        if diff["current_version"] is None:
            return "<p>New plan; all modules will be added.</p>"

        def module_label(module: dict[str, Any]) -> str:
            label = module.get("item") or module.get("label") or module.get("id", "")
            slot = module.get("slot")
            return escape(f"{label} ({slot})" if slot else str(label))

        sections = [
            ("Added", diff["added"], "added"),
            ("Removed", diff["removed"], "removed"),
        ]
        body = "".join(
            f"<p><strong>{title} ({len(modules)})</strong></p><ul>"
            + "".join(f"<li>{module_label(module)}</li>" for module in modules)
            + "</ul>"
            for title, modules, _ in sections
            if modules
        )
        changed_body = "".join(
            f"<li>{module_label(item['before'])} → {module_label(item['after'])}</li>"
            for item in diff["changed"]
        )
        if changed_body:
            body += f"<p><strong>Changed ({len(diff['changed'])})</strong></p><ul>{changed_body}</ul>"
        return (
            f"<p>Version {diff['current_version']} → {diff['next_version']}</p>"
            + (body or "<p>No module changes.</p>")
        )

    def _on_event(self, event: Event, _context: dict[str, Any]) -> None:
        if not isinstance(event, GameEvent):
            return
        event_name = event.content.get("event")
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
            event.content.get("ShipID")
            or event.content.get("ShipIdent")
            or event.content.get("ShipName")
            or event.content.get("Ship")
            or ""
        )
        if ship_id:
            self._current_ship_id = ship_id
        modules = self._event_modules(event.content)
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
            db.execute(
                """
                INSERT INTO active_session
                    (id, plan_id, ship_instance_id, ship_custom_name,
                     completed_steps, session_start, last_activity,
                     plan_version_at_session_start, paused)
                VALUES (?, ?, ?, ?, '[]', ?, ?, ?, 0)
                ON CONFLICT(id) DO UPDATE SET
                    plan_id = excluded.plan_id,
                    ship_instance_id = excluded.ship_instance_id,
                    ship_custom_name = excluded.ship_custom_name,
                    current_step = 0,
                    completed_steps = '[]',
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
        self._republish()

    def _republish(self) -> None:
        """Push mutated field rows/paragraphs to the UI when the runtime is up.

        The manager republishes the whole settings config; persisting the
        summary key is only the trigger. In config state there is no manager
        handle, and button hooks are republished by the manager itself.
        """
        if self.helper is None:
            return
        self.helper._plugin_manager.update_plugin_setting(
            self.plugin_manifest.guid,
            "session_summary",
            self.settings.get("session_summary", ""),
        )

    def _plan_module_count(self, plan_id: str) -> int:
        with self.get_db() as db:
            row = db.execute(
                "SELECT source_json FROM plans WHERE id = ?", (plan_id,)
            ).fetchone()
        if row is None:
            return 0
        return len(self._plan_steps(row["source_json"]))
